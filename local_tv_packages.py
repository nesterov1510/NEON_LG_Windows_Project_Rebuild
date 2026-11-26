"""Validation of user-selected TV application packages (no extraction)."""
import io
import os
import re
import stat
import xml.etree.ElementTree as ET
import zipfile

MAX_PACKAGE = 128 * 1024 * 1024
MAX_UNPACKED = 256 * 1024 * 1024


def read_package(path, extension):
    if not isinstance(path, str) or not path.lower().endswith(extension):
        raise ValueError('Select a ' + extension + ' file.')
    if not os.path.isfile(path):
        raise ValueError('Package file was not found.')
    with open(path, 'rb') as f:
        data = f.read(MAX_PACKAGE + 1)
    if not data or len(data) > MAX_PACKAGE:
        raise ValueError('Package is empty or larger than 128 MB.')
    return data


def validate_ipk(data):
    if not data.startswith(b'!<arch>\n'):
        raise ValueError('This is not an IPK (ar archive).')
    pos, names = 8, []
    while pos < len(data):
        header = data[pos:pos + 60]
        if len(header) != 60 or header[58:60] != b'`\n':
            raise ValueError('Invalid IPK archive header.')
        try:
            size = int(header[48:58].decode('ascii').strip())
            name = header[:16].decode('ascii').strip().rstrip('/')
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Invalid IPK archive header.') from exc
        if size < 0 or pos + 60 + size > len(data):
            raise ValueError('Truncated IPK archive.')
        names.append(name)
        pos += 60 + size + (size % 2)
    if pos != len(data) or not any(n.startswith('control.tar.') for n in names) or not any(n.startswith('data.tar.') for n in names) or 'debian-binary' not in names:
        raise ValueError('IPK is missing required archive members.')


def validate_wgt(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > 10000 or sum(i.file_size for i in infos) > MAX_UNPACKED:
                raise ValueError('WGT archive exceeds safe size limits.')
            names = set()
            for info in infos:
                n = info.filename
                raw_name = info.orig_filename
                if (raw_name != n or not n or n.startswith(('/', '\\')) or '\\' in n or
                    any(part in ('.', '..') for part in n.split('/')) or
                    re.match(r'^[A-Za-z]:', n) or info.flag_bits & 1 or
                    stat.S_ISLNK(info.external_attr >> 16) or n in names):
                    raise ValueError('WGT contains unsafe, encrypted or duplicate paths.')
                names.add(n)
            if 'config.xml' not in names or z.getinfo('config.xml').file_size > 1024 * 1024:
                raise ValueError('WGT must contain a valid config.xml at its root.')
            xml = z.read('config.xml')
            try:
                xml_text = xml.decode('utf-8-sig')
            except UnicodeError as exc:
                raise ValueError('WGT manifest must be UTF-8.') from exc
            if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', xml_text, re.I):
                raise ValueError('WGT manifest must not contain XML entities or a DTD.')
            root = ET.fromstring(xml_text)
            if root.tag != '{http://www.w3.org/ns/widgets}widget':
                raise ValueError('Not a Tizen widget manifest.')
            app = root.find('{http://tizen.org/ns/widgets}application')
            if app is None:
                raise ValueError('Only Tizen web WGT packages with tizen:application are supported.')
            pkg = app.get('package', '')
            app_id = app.get('id', '')
            if not re.fullmatch(r'[A-Za-z0-9]{10}', pkg) or not re.fullmatch(re.escape(pkg) + r'\.[A-Za-z0-9_-]+', app_id):
                raise ValueError('Invalid Tizen package or application ID.')
            return pkg, app_id
    except (zipfile.BadZipFile, ET.ParseError, KeyError, RuntimeError) as exc:
        raise ValueError('Invalid WGT archive or config.xml.') from exc


_TIZEN_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,128}$')


def validate_tizen_package(data, allow_tpk=True):
    """Validate a Samsung Tizen .wgt or .tpk archive safely and extract manifest metadata."""
    if not data or len(data) > MAX_PACKAGE:
        raise ValueError('Package is empty or larger than 128 MB.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > 10000 or sum(i.file_size for i in infos) > MAX_UNPACKED:
                raise ValueError('Tizen package exceeds safe size limits.')
            names = set()
            for info in infos:
                n = info.filename
                raw_name = info.orig_filename
                if (raw_name != n or not n or n.startswith(('/', '\\')) or '\\' in n or
                    any(part in ('.', '..') for part in n.split('/')) or
                    re.match(r'^[A-Za-z]:', n) or info.flag_bits & 1 or
                    stat.S_ISLNK(info.external_attr >> 16) or n in names):
                    raise ValueError('Tizen package contains unsafe, encrypted or duplicate paths.')
                names.add(n)

            if 'config.xml' in names:
                if z.getinfo('config.xml').file_size > 1024 * 1024:
                    raise ValueError('config.xml exceeds 1 MB limit.')
                xml_bytes = z.read('config.xml')
                try:
                    xml_text = xml_bytes.decode('utf-8-sig')
                except UnicodeError as exc:
                    raise ValueError('Tizen manifest must be UTF-8.') from exc
                if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', xml_text, re.I):
                    raise ValueError('Tizen manifest must not contain XML entities or a DTD.')
                root = ET.fromstring(xml_text)
                if root.tag != '{http://www.w3.org/ns/widgets}widget':
                    raise ValueError('Not a Tizen widget manifest.')
                app = root.find('{http://tizen.org/ns/widgets}application')
                if app is None:
                    raise ValueError('WGT config.xml is missing tizen:application.')
                pkg = (app.get('package') or '').strip()
                app_id = (app.get('id') or '').strip()
                if not _TIZEN_ID_RE.fullmatch(pkg) or not _TIZEN_ID_RE.fullmatch(app_id):
                    raise ValueError('Invalid Tizen package or application ID.')
                return {
                    'kind': 'wgt',
                    'pkg_id': pkg,
                    'app_id': app_id,
                    'version': (root.get('version') or '').strip(),
                    'required_version': (app.get('required_version') or '').strip(),
                }

            if allow_tpk and 'tizen-manifest.xml' in names:
                if z.getinfo('tizen-manifest.xml').file_size > 1024 * 1024:
                    raise ValueError('tizen-manifest.xml exceeds 1 MB limit.')
                xml_bytes = z.read('tizen-manifest.xml')
                try:
                    xml_text = xml_bytes.decode('utf-8-sig')
                except UnicodeError as exc:
                    raise ValueError('Tizen manifest must be UTF-8.') from exc
                if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', xml_text, re.I):
                    raise ValueError('Tizen manifest must not contain XML entities or a DTD.')
                root = ET.fromstring(xml_text)
                if root.tag.split('}')[-1] != 'manifest':
                    raise ValueError('Not a native TPK manifest.')
                pkg = (root.get('package') or '').strip()
                app_id = ''
                for el in root.iter():
                    local_tag = el.tag.split('}')[-1]
                    if local_tag in ('ui-application', 'service-application', 'widget-application', 'watch-application'):
                        cand = (el.get('appid') or '').strip()
                        if cand:
                            app_id = cand
                            break
                if not _TIZEN_ID_RE.fullmatch(pkg) or not _TIZEN_ID_RE.fullmatch(app_id):
                    raise ValueError('Invalid TPK package or application ID.')
                return {
                    'kind': 'tpk',
                    'pkg_id': pkg,
                    'app_id': app_id,
                    'version': (root.get('version') or '').strip(),
                    'required_version': (root.get('api-version') or '').strip(),
                }

            raise ValueError('Package must contain config.xml (.wgt) or tizen-manifest.xml (.tpk).')
    except ValueError:
        raise
    except (zipfile.BadZipFile, ET.ParseError, KeyError, RuntimeError) as exc:
        raise ValueError('Invalid Tizen package archive or manifest.') from exc

