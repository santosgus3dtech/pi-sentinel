import json

from pisentinel.printers.discovery import CrealityK1CDiscoveryService, DiscoveryHttpResponse
from pisentinel.probes import ProbeResult

HOST = "192.168.10.155"


def tcp_probe(*open_ports):
    ports = set(open_ports)

    def probe(target):
        return ProbeResult(target["port"] in ports)

    return probe


def fetcher(responses, calls=None):
    def fetch(url, method, timeout, max_bytes):
        if calls is not None:
            calls.append((url, method, timeout, max_bytes))
        response = responses.get(url)
        if response is None:
            raise OSError("unavailable")
        return response

    return fetch


def response(url, body, content_type="text/html", status=200):
    if isinstance(body, str):
        body = body.encode()
    return DiscoveryHttpResponse(url, status, {"content-type": content_type}, body)


def service(*, ping_success=True, ports=(), responses=None, calls=None, websocket_probe=None):
    return CrealityK1CDiscoveryService(
        HOST,
        ping_probe=lambda _host: ProbeResult(ping_success),
        tcp_probe=tcp_probe(*ports),
        http_fetcher=fetcher(responses or {}, calls),
        websocket_probe=websocket_probe or (lambda _url, _timeout: False),
    )


def test_host_offline_does_not_attempt_http():
    calls = []
    result = service(ping_success=False, responses={}, calls=calls).discover()

    assert result["reachable"] is False
    assert result["services"]["http"]["available"] is False
    assert result["api"]["state"] == "not_found"
    assert calls == []


def test_http_only_reports_web_interface_without_inventing_api():
    root = f"http://{HOST}/"
    result = service(
        ports=(80,),
        responses={root: response(root, "<html><title>Creality</title></html>")},
    ).discover()

    assert result["services"]["http"] == {
        "available": True,
        "state": "available",
        "port": 80,
        "status": 200,
        "content_type": "text/html",
        "server": None,
        "title": "Creality",
    }
    assert result["api"]["available"] is False
    assert result["moonraker"]["available"] is False


def test_structured_api_is_found_from_interface_javascript():
    root = f"http://{HOST}/"
    asset = f"http://{HOST}/assets/app.js"
    endpoint = f"http://{HOST}/api/status"
    result = service(
        ports=(80,),
        responses={
            root: response(root, '<script src="/assets/app.js"></script>'),
            asset: response(asset, 'fetch("/api/status")', "application/javascript"),
            endpoint: response(endpoint, '{"state":"idle"}', "application/json"),
        },
    ).discover()

    assert result["api"]["available"] is True
    assert result["api"]["type"] == "structured_json"
    assert result["api"]["url"] == endpoint
    assert result["web"]["assets"] == [asset]


def test_camera_stream_is_confirmed_by_content_type():
    root = f"http://{HOST}/"
    camera = f"http://{HOST}/webcam/?action=stream"
    result = service(
        ports=(80,),
        responses={
            root: response(root, f'<script>const camera="{camera}";</script>'),
            camera: response(camera, b"--frame", "multipart/x-mixed-replace; boundary=frame"),
        },
    ).discover()

    assert result["camera"]["detected"] is True
    assert result["camera"]["available"] is True
    assert result["camera"]["protocol"] == "mjpeg"
    assert result["camera"]["url"] == camera


def test_partial_services_do_not_turn_open_port_into_moonraker_claim():
    https_root = f"https://{HOST}/"
    socket_url = f"wss://{HOST}/socket"
    moonraker_info = f"http://{HOST}:7125/server/info"
    result = service(
        ping_success=False,
        ports=(443, 7125),
        responses={
            https_root: response(https_root, f'<script>new WebSocket("{socket_url}")</script>'),
            moonraker_info: response(moonraker_info, '{"result":{"name":"other"}}', "application/json"),
        },
        websocket_probe=lambda url, timeout: url == socket_url and timeout == 2.0,
    ).discover()

    assert result["reachable"] is True
    assert result["services"]["https"]["available"] is True
    assert result["websocket"]["available"] is True
    assert result["moonraker"]["available"] is False
    assert "nenhuma assinatura Moonraker" in result["moonraker"]["evidence"][0]


def test_moonraker_requires_a_recognizable_structured_signature():
    url = f"http://{HOST}:7125/server/info"
    payload = {"result": {"moonraker_version": "0.9", "klippy_connected": True}}
    result = service(
        ports=(7125,),
        responses={url: response(url, json.dumps(payload), "application/json")},
    ).discover()

    assert result["moonraker"]["available"] is True
    assert result["api"]["type"] == "moonraker"


def test_sensitive_values_are_redacted_from_result():
    root = f"http://{HOST}/"
    secret_url = f"http://{HOST}/api/status?token=do-not-show"
    path_secret_url = f"http://{HOST}/api/token/also-do-not-show/status"
    result = service(
        ports=(80,),
        responses={
            root: response(root, f'<script>fetch("{secret_url}"); fetch("{path_secret_url}")</script>'),
            secret_url: response(secret_url, "{}", "application/json"),
            path_secret_url: response(path_secret_url, "{}", "application/json"),
        },
    ).discover()

    serialized = json.dumps(result)
    assert "do-not-show" not in serialized
    assert "also-do-not-show" not in serialized
    assert "redacted" in serialized


def test_dynamic_stock_firmware_urls_are_resolved_to_the_printer_host():
    root = f"http://{HOST}/"
    asset = f"http://{HOST}/static/js/app.js"
    camera = f"http://{HOST}:8080/?action=stream"
    websocket = f"ws://{HOST}:9999/"
    result = service(
        ports=(80, 8080, 9999),
        responses={
            root: response(root, '<script src="/static/js/app.js"></script>'),
            asset: response(asset, 'const ua=location.hostname; new WebSocket(`ws://${ua}:9999`); const c=`http://${e.$ip}:8080/?action=stream`', "application/javascript"),
            camera: response(camera, b"--frame", "multipart/x-mixed-replace; boundary=frame"),
        },
        websocket_probe=lambda url, timeout: url == websocket,
    ).discover()

    assert result["websocket"]["available"] is True
    assert result["websocket"]["urls"] == [websocket]
    assert result["camera"]["protocol"] == "mjpeg"
