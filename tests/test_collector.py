import subprocess
from unittest.mock import Mock

import pytest

from pisentinel import probes


def test_discovery_rejects_outside_scope():
    output = '192.168.10.8 aa:bb:cc:dd:ee:01 Printer\n192.168.99.1 aa:bb:cc:dd:ee:02 Router\n192.168.10.255 aa:bb:cc:dd:ee:03 Bad\nnoise\n'
    assert probes.parse_arp_scan(output, '192.168.10.0/24') == {'192.168.10.8': 'aa:bb:cc:dd:ee:01'}


def test_discovery_retains_vendor_without_trusting_unknown_labels():
    output = ('192.168.10.8 10:2b:41:00:00:01 Samsung Electronics Co.,Ltd\n'
              '192.168.10.9 02:11:22:33:44:09 (Unknown: locally administered)\n')
    assert probes.parse_arp_scan_details(output, '192.168.10.0/24') == {
        '192.168.10.8': {'mac': '10:2b:41:00:00:01', 'vendor': 'Samsung Electronics Co.,Ltd'},
        '192.168.10.9': {'mac': '02:11:22:33:44:09', 'vendor': None},
    }


@pytest.mark.parametrize('network', ['0.0.0.0/0', '8.8.8.0/24', '127.0.0.0/24', '169.254.0.0/24', '192.168.0.0/16', '::1/128'])
def test_discovery_refuses_public_and_oversized_networks(network):
    with pytest.raises(ValueError):
        probes.validate_network(network)


def test_windows_unreachable_is_not_success(monkeypatch):
    monkeypatch.setattr(probes.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(probes.subprocess, 'run', lambda *a, **k: Mock(returncode=0, stdout='Resposta de 192.168.10.1: Host de destino inacessível.', stderr=''))
    assert probes.ping('192.168.10.90').success is False


def test_localized_windows_ping_rtt(monkeypatch):
    monkeypatch.setattr(probes.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(probes.subprocess, 'run', lambda *a, **k: Mock(returncode=0, stdout='Resposta de 192.168.10.1: bytes=32 tempo<1ms TTL=64', stderr=''))
    result = probes.ping('192.168.10.1')
    assert result.success and result.rtt_ms == .5


def test_missing_ping_is_collector_failure_not_host_outage(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(probes.subprocess, 'run', missing)
    with pytest.raises(probes.ProbeUnavailable):
        probes.ping('192.168.10.1')


def test_arp_does_not_scan_when_no_known_devices(monkeypatch):
    monkeypatch.setattr(probes, 'run', lambda *a, **k: pytest.fail('should not invoke a scanner'))
    assert probes.arp_discover({'cidr': '192.168.10.0/24', 'interface': 'eth0'}, []) == {}


def test_ping_command_is_argument_list(monkeypatch):
    captured = []
    monkeypatch.setattr(probes.platform, 'system', lambda: 'Linux')
    def command(args, **kwargs):
        captured.append((args, kwargs))
        return Mock(returncode=0, stdout='64 bytes time=1.75 ms ttl=64', stderr='')
    monkeypatch.setattr(probes.subprocess, 'run', command)
    assert probes.ping('192.168.10.1').rtt_ms == 1.75
    assert captured[0][0][-1] == '192.168.10.1'
    assert not captured[0][1].get('shell')


def test_tcp_target_resolves_before_socket(monkeypatch):
    seen = []
    monkeypatch.setattr(probes, 'resolve_host', lambda host: '203.0.113.10')
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): return None
    monkeypatch.setattr(probes.socket, 'create_connection', lambda address, timeout: seen.append((address, timeout)) or Connection())
    result = probes.probe_target({'kind': 'tcp', 'host': 'example.test', 'port': 443})
    assert result.success
    assert seen == [(('203.0.113.10', 443), 2)]


def test_windows_arp_timeout_is_collector_failure(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
    monkeypatch.setattr(probes.subprocess, 'run', timeout)
    with pytest.raises(probes.ProbeUnavailable, match='tempo limite'):
        probes.windows_arp_bounded(['192.168.10.1'], timeout=1)


def test_windows_wifi_tool_is_optional_with_authorized_cidr(monkeypatch):
    monkeypatch.setattr(probes.platform, 'system', lambda: 'Windows')
    def command(args, timeout=8):
        if args[0] == 'powershell.exe':
            return '{"interface":"Ethernet","ip":"192.168.10.10","prefix":24,"gateway":"192.168.10.1"}'
        raise probes.ProbeUnavailable('Wi-Fi service unavailable')
    monkeypatch.setattr(probes, 'run', command)
    detected = probes.detect_network('192.168.10.0/24', 'Ethernet')
    assert detected['connected_ssid'] is None
