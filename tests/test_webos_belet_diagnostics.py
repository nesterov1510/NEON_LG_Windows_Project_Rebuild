import json
from unittest import mock

from core import Api


def test_belet_launch_reports_webos_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    api = Api()
    api.client = mock.MagicMock()
    def rejected(command, lines):
        lines.extend(json.dumps({'returnValue': False, 'errorText': 'App not found'}, indent=2).splitlines())
        return 0
    with mock.patch.object(api, '_exec', side_effect=rejected):
        result = api.launch_belet_app()
    assert result['ok'] is False
    assert result['error'] == 'App not found'
    api.client = None
    api.close()


def test_belet_installer_allows_webos_luna_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    api = Api()
    api.client = mock.MagicMock()
    api.client.get_transport.return_value.is_active.return_value = True
    with mock.patch.object(api, '_exec', return_value=0) as execute:
        result = api.install_belet_app()
    assert result['ok'] is True
    assert execute.call_args.kwargs['timeout'] > 35
    api.client = None
    api.close()
