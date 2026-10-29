from src.brain.mcp.tools import icloud_read

HEADERS = b"Date: Thu, 24 Sep 2026 22:06:41 +0000\r\nFrom: Apple <a@example.com>\r\nSubject: Hello\r\n\r\n"
BODY = b"body preview"


class FakeIMAP:
    def __init__(self, data):
        self.data = data

    def uid(self, *args):
        return ("OK", self.data)


def test_envelope_reads_headers_when_body_comes_first():
    # Gmail's order: body section first, then the header section.
    m = FakeIMAP([
        (b"2 (UID 7 BODY[TEXT]<0> {12}", BODY),
        (b" BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {90}", HEADERS),
        b")",
    ])
    line = icloud_read._fetch_envelope(m, b"7")
    assert "subject=[Hello]" in line
    assert "a@example.com" in line
    assert "snippet=[body preview]" in line


def test_envelope_reads_headers_when_headers_come_first():
    m = FakeIMAP([
        (b"2 (UID 7 BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {90}", HEADERS),
        (b" BODY[TEXT]<0> {12}", BODY),
        b")",
    ])
    line = icloud_read._fetch_envelope(m, b"7")
    assert "subject=[Hello]" in line
    assert "snippet=[body preview]" in line
