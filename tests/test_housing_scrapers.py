from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.orchestrator.config import HOUSING_CORRIDOR

if not HOUSING_CORRIDOR.configured:  # pragma: no cover
    pytest.skip(
        "housing_corridor not configured (private user/ overlay absent)",
        allow_module_level=True,
    )

from src.brain.housing.models import SearchCriteria  # noqa: E402
from src.brain.housing.scrapers import immowelt, kleinanzeigen  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "housing"


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class _FakeClient:
    def __init__(self, routes: dict[str, str]) -> None:
        self._routes = routes

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def get(self, url: str, **_kwargs):  # noqa: ANN001
        for host, html in self._routes.items():
            if host in url:
                return _FakeResponse(html)
        return _FakeResponse("", status_code=404)


def _patch_client(monkeypatch, module, html: str) -> None:
    host = getattr(module, "BASE", "").replace("https://", "").replace("http://", "") or ""
    routes = {host: html} if host else {"": html}
    monkeypatch.setattr(module._common, "client", lambda: _FakeClient(routes))


def _patch_multi(monkeypatch, **module_html) -> None:
    routes: dict[str, str] = {}
    for mod_name, html in module_html.items():
        from importlib import import_module
        mod = import_module(f"src.brain.housing.scrapers.{mod_name}")
        host = getattr(mod, "BASE", "").replace("https://", "").replace("http://", "")
        if host:
            routes[host] = html
    from src.brain.housing.scrapers import _common
    monkeypatch.setattr(_common, "client", lambda: _FakeClient(routes))


def _read_fixture(name: str) -> str:
    path = FIXTURES / name
    return path.read_text(encoding="utf-8")


def test_kleinanzeigen_fixture_parses_listings(monkeypatch) -> None:
    _patch_client(monkeypatch, kleinanzeigen, _read_fixture("kleinanzeigen.html"))
    criteria = SearchCriteria(area="musterstadt", max_price=600_000)
    listings = asyncio.run(kleinanzeigen.search(criteria))

    assert len(listings) >= 3, f"expected ≥3 Kleinanzeigen listings, got {len(listings)}"

    for lst in listings:
        assert lst.source == "kleinanzeigen"
        assert lst.source_id, "every listing needs a stable source_id"
        assert lst.url.startswith("https://www.kleinanzeigen.de/"), lst.url
        assert lst.title, "title must not be empty"


def test_kleinanzeigen_extracts_price_for_at_least_some_listings(monkeypatch) -> None:
    _patch_client(monkeypatch, kleinanzeigen, _read_fixture("kleinanzeigen.html"))
    listings = asyncio.run(kleinanzeigen.search(SearchCriteria(area="musterstadt")))
    priced = [lst for lst in listings if lst.price_eur]
    assert priced, "no listing carried a numeric price — price parser likely broken"


def test_kleinanzeigen_max_price_filter_works(monkeypatch) -> None:
    _patch_client(monkeypatch, kleinanzeigen, _read_fixture("kleinanzeigen.html"))
    cheap = asyncio.run(kleinanzeigen.search(SearchCriteria(area="musterstadt", max_price=400_000)))
    expensive = asyncio.run(kleinanzeigen.search(SearchCriteria(area="musterstadt", max_price=900_000)))
    assert len(cheap) <= len(expensive), "tighter max_price returned MORE listings"
    for lst in cheap:
        if lst.price_eur is not None:
            assert lst.price_eur <= 400_000


def test_immowelt_fixture_parses_listings(monkeypatch) -> None:
    _patch_client(monkeypatch, immowelt, _read_fixture("immowelt.html"))
    criteria = SearchCriteria(area="musterstadt", max_price=550_000)
    listings = asyncio.run(immowelt.search(criteria))

    assert len(listings) >= 20, (
        f"expected ≥20 Immowelt listings (UFRN parser must be working); got {len(listings)}"
    )

    for lst in listings:
        assert lst.source == "immowelt"
        assert lst.source_id, "every listing needs a stable source_id"
        assert lst.url.startswith("https://www.immowelt.de/"), lst.url
        assert lst.title, "title must not be empty"


def test_immowelt_fixture_stays_minimal_and_synthetic() -> None:
    path = FIXTURES / "immowelt.html"
    html = path.read_text(encoding="utf-8")
    classifieds = immowelt._extract_classifieds(html)

    assert path.stat().st_size < 10_000
    assert "userId" not in html
    assert "__UFRN_LIFECYCLE_SERVERREQUEST__" not in html
    assert set(classifieds) == {f"synthetic-{index:02d}" for index in range(1, 21)}
    assert {
        classified["location"]["address"]["city"]
        for classified in classifieds.values()
    } == {"Musterstadt"}


def test_immowelt_extracts_rooms_and_sqm(monkeypatch) -> None:
    _patch_client(monkeypatch, immowelt, _read_fixture("immowelt.html"))
    listings = asyncio.run(immowelt.search(SearchCriteria(area="musterstadt", max_price=550_000)))

    with_rooms = [lst for lst in listings if lst.rooms is not None]
    with_sqm = [lst for lst in listings if lst.sqm is not None]
    assert len(with_rooms) / max(len(listings), 1) >= 0.7, "rooms parser miss-rate too high"
    assert len(with_sqm) / max(len(listings), 1) >= 0.7, "sqm parser miss-rate too high"


def test_immowelt_min_rooms_filter_works(monkeypatch) -> None:
    _patch_client(monkeypatch, immowelt, _read_fixture("immowelt.html"))
    big = asyncio.run(immowelt.search(SearchCriteria(area="musterstadt", max_price=550_000, min_rooms=4)))
    everything = asyncio.run(immowelt.search(SearchCriteria(area="musterstadt", max_price=550_000)))
    assert len(big) <= len(everything)
    for lst in big:
        if lst.rooms is not None:
            assert lst.rooms >= 4


def test_immowelt_handles_layout_change_gracefully(monkeypatch) -> None:
    _patch_client(monkeypatch, immowelt, "<html><body>nothing here</body></html>")
    listings = asyncio.run(immowelt.search(SearchCriteria(area="musterstadt", max_price=550_000)))
    assert listings == []


def test_search_all_returns_combined_listings(monkeypatch) -> None:
    from src.brain.housing import scrapers

    _patch_multi(
        monkeypatch,
        kleinanzeigen=_read_fixture("kleinanzeigen.html"),
        immowelt=_read_fixture("immowelt.html"),
    )
    listings, _ = asyncio.run(scrapers.search_all_reporting(SearchCriteria(area="musterstadt", max_price=550_000)))

    sources = {lst.source for lst in listings}
    assert "immowelt" in sources, "immowelt missing from fan-out"
    assert "kleinanzeigen" in sources, "kleinanzeigen missing from fan-out"
    assert len(listings) >= 20


def test_kleinanzeigen_reads_the_2026_card_layout(monkeypatch) -> None:
    _patch_client(monkeypatch, kleinanzeigen, _read_fixture("kleinanzeigen_2026.html"))
    listings = asyncio.run(kleinanzeigen.search(SearchCriteria(area="musterstadt")))
    by_id = {lst.source_id: lst for lst in listings}
    first, second = by_id["9000000001"], by_id["9000000002"]
    assert first.title == "Reihenhaus mit Garten in Musterstadt"
    assert first.url == "https://www.kleinanzeigen.de/s-anzeige/reihenhaus-mit-garten-in-musterstadt/9000000001-208-1"
    assert (first.price_eur, first.rooms, first.sqm, first.postcode) == (489_000, 5, 128, "12345")
    assert first.city == "musterstadt"
    assert (second.price_eur, second.rooms, second.sqm) == (720_000, 6, 142.5)


def test_kleinanzeigen_page_it_cannot_read_is_an_error(monkeypatch) -> None:
    page = '<html><body><div><a href="/s-anzeige/haus/1-208-1">Haus</a></div></body></html>'
    _patch_client(monkeypatch, kleinanzeigen, page)
    with pytest.raises(RuntimeError, match="layout"):
        asyncio.run(kleinanzeigen.search(SearchCriteria(area="musterstadt")))


def test_a_blocked_or_broken_source_is_reported_not_counted_as_empty(monkeypatch) -> None:
    from src.brain.housing.scrapers import _common, search_all_reporting

    class Blocked(_FakeClient):
        async def get(self, url, **_kwargs):
            if "immowelt" in url:
                return _FakeResponse("", status_code=403)
            return _FakeResponse(_read_fixture("kleinanzeigen_2026.html"))

    monkeypatch.setattr(_common, "client", lambda: Blocked({}))
    listings, failures = asyncio.run(search_all_reporting(SearchCriteria(area="musterstadt")))
    assert {lst.source for lst in listings} == {"kleinanzeigen"}
    assert set(failures) == {"immowelt"} and "403" in failures["immowelt"]


def test_the_scraper_client_never_follows_redirects():
    from src.brain.housing.scrapers import _common

    client = _common.client()
    try:
        assert client.follow_redirects is False
        assert client.trust_env is False
    finally:
        asyncio.run(client.aclose())
