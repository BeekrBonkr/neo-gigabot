from types import SimpleNamespace

from utils.config import _http_url
from utils.website import site_button, site_link, site_url


def _bot(url):
    return SimpleNamespace(config=SimpleNamespace(website_url=url))


def test_links_are_built_from_the_configured_address():
    bot = _bot("https://example.com")
    assert site_url(bot) == "https://example.com" and site_url(bot, "/app/1") == "https://example.com/app/1"
    assert site_link(bot, "website", "/playlists") == "[website](https://example.com/playlists)"
    assert site_button(bot, "Open", "/app/1").url == "https://example.com/app/1"


def test_no_website_means_no_links_anywhere():
    for bot in (_bot(""), SimpleNamespace()):
        assert site_url(bot, "/app/1") == "" and site_link(bot, "website") == "" and site_button(bot, "Open") is None


def test_only_http_addresses_are_accepted_from_the_environment():
    assert _http_url(" https://example.com/ ") == "https://example.com"
    assert _http_url("javascript:alert(1)") == "" and _http_url("example.com") == "" and _http_url("") == ""
