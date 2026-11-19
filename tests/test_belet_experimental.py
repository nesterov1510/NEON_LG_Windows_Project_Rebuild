import io
import json
import tarfile
from core import Api
import belet_experimental


def members(ipk):
    pos = 8
    while pos < len(ipk):
        header = ipk[pos:pos + 60]
        size = int(header[48:58].decode().strip())
        name = header[:16].decode().strip().rstrip('/')
        data = ipk[pos + 60:pos + 60 + size]
        yield name, data
        pos += 60 + size + (size % 2)


def test_experimental_package_is_separate_and_contains_no_credentials():
    api = Api()
    files = dict(members(api.build_belet_ipk(experimental=True)))
    with tarfile.open(fileobj=io.BytesIO(files['data.tar.gz']), mode='r:gz') as archive:
        base = 'usr/palm/applications/tm.belet.neontest/'
        info = json.load(archive.extractfile(base + 'appinfo.json'))
        page = archive.extractfile(base + 'index.html').read().decode('utf-8')
    assert info['id'] == 'tm.belet.neontest'
    assert 'https://homepage.belet.me/api/v2/home_page' in page
    assert 'Вход и просмотр фильмов' in page
    assert 'authorization' not in page.lower()
    assert 'https://belet.tm/' in page


def test_default_app_is_not_replaced():
    files = dict(members(Api().build_belet_ipk()))
    with tarfile.open(fileobj=io.BytesIO(files['data.tar.gz']), mode='r:gz') as archive:
        info = json.load(archive.extractfile('usr/palm/applications/tm.belet.webos/appinfo.json'))
    assert info['id'] == 'tm.belet.webos'
