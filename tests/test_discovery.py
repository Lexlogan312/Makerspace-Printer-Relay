import ipaddress
import socket
import time

from relay.discovery import Discovery, parse_announcement

# Captured from FlintLockwood with tcpdump on the makerspace network
PACKET = (
    b"NOTIFY * HTTP/1.1\r\n"
    b"HOST: 239.255.255.250:1900\r\n"
    b"Server: UPnP/1.0\r\n"
    b"Location: 172.20.10.9\r\n"
    b"NT: urn:bambulab-com:device:3dprinter:1\r\n"
    b"USN: 03900D5C2000916\r\n"
    b"Cache-Control: max-age=1800\r\n"
    b"DevModel.bambu.com: N2S\r\n"
    b"DevName.bambu.com: 3DP-039-916\r\n"
    b"DevSignal.bambu.com: -49\r\n"
    b"DevConnect.bambu.com: lan\r\n"
    b"DevBind.bambu.com: free\r\n"
    b"Devseclink.bambu.com: secure\r\n"
    b"DevVersion.bambu.com: 01.08.00.00\r\n"
    b"DevCap.bambu.com: 1\r\n\r\n"
)


def test_parses_real_announcement():
    ann = parse_announcement(PACKET, "172.20.10.9", seen_at=1.0)
    assert ann.serial == "03900D5C2000916"
    assert ann.ip == "172.20.10.9"
    assert ann.model_code == "N2S" and ann.model == "A1"
    assert ann.dev_name == "3DP-039-916"
    assert ann.firmware == "01.08.00.00"


def test_rejects_location_that_doesnt_match_sender():
    assert parse_announcement(PACKET, "172.20.10.50", seen_at=1.0) is None


def test_rejects_other_packets():
    assert parse_announcement(b"M-SEARCH * HTTP/1.1\r\n\r\n", "172.20.10.9", 1.0) is None
    assert parse_announcement(PACKET.replace(b"bambulab", b"other"), "172.20.10.9", 1.0) is None
    assert parse_announcement(b"\xff\xfe garbage", "172.20.10.9", 1.0) is None


def test_unknown_model_code_has_no_name():
    ann = parse_announcement(PACKET.replace(b"N2S", b"ZZ9"), "172.20.10.9", 1.0)
    assert ann.model_code == "ZZ9" and ann.model is None


def _send_to_listener(discovery: Discovery, packet: bytes) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(packet, ("127.0.0.1", discovery.port))


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(fn, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.02)
    return False


def test_listener_records_announcements_from_its_network():
    d = Discovery(network=ipaddress.IPv4Network("127.0.0.0/8"), port=_free_udp_port())
    d.start()
    try:
        assert d.available
        _send_to_listener(d, PACKET.replace(b"172.20.10.9", b"127.0.0.1"))
        assert _wait_for(lambda: d.get("03900D5C2000916") is not None)
        assert d.get("03900D5C2000916").ip == "127.0.0.1"
        assert [a.serial for a in d.recent()] == ["03900D5C2000916"]
    finally:
        d.stop()


def test_listener_ignores_other_networks():
    d = Discovery(network=ipaddress.IPv4Network("10.42.0.0/24"), port=_free_udp_port())
    d.start()
    try:
        _send_to_listener(d, PACKET.replace(b"172.20.10.9", b"127.0.0.1"))
        assert not _wait_for(lambda: d.get("03900D5C2000916") is not None, timeout=0.5)
    finally:
        d.stop()
