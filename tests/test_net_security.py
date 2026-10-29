import ast
from pathlib import Path

import pytest

from src.brain.net_security import read_bounded, safe_https_url


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/x",
        "https://user:secret@example.test/x",
        "https://example.test:8443/x",
        "https://example.test:bad/x",
        "https://example.test/x\nInjected: value",
        "https://example.test/x\tInjected",
        "file:///etc/passwd",
        "",
    ],
)
def test_safe_https_url_rejects_unsafe_values(url: str) -> None:
    assert safe_https_url(url) is None


def test_safe_https_url_supports_exact_host_allowlists() -> None:
    allowed = frozenset({"files.example.test"})
    assert safe_https_url(
        " https://files.example.test:443/x ", allowed_hosts=allowed
    ) == "https://files.example.test:443/x"
    assert safe_https_url("https://evil.test/x", allowed_hosts=allowed) is None
    assert safe_https_url("https://files.example.test./x", allowed_hosts=allowed) is None


def test_http_clients_do_not_inherit_ambient_proxies() -> None:
    root = Path(__file__).parents[1]
    constructors = {"AsyncClient", "Client", "get", "post", "put", "patch", "delete", "request"}
    for path in (*root.joinpath("src").rglob("*.py"), *root.joinpath("scripts").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if not isinstance(owner, ast.Name) or owner.id != "httpx":
                continue
            if node.func.attr not in constructors:
                continue
            values = {item.arg: item.value for item in node.keywords}
            assert isinstance(values.get("trust_env"), ast.Constant), path
            assert values["trust_env"].value is False, path


def test_read_bounded_exposes_one_overflow_byte() -> None:
    import asyncio

    async def read() -> bytes:
        stream = asyncio.StreamReader()
        stream.feed_data(b"123456")
        stream.feed_eof()
        return await read_bounded(stream, 4)

    assert asyncio.run(read()) == b"12345"
