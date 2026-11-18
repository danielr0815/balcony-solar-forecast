"""Defensive same-origin Bearer handling exercised without network calls."""

from urllib.request import Request

import pytest

from scripts.validation.bsf_fetch import FetchError, Rest, _OriginRedirect


@pytest.mark.parametrize("url", [
    "https://another-host.invalid/config",
    "http://ha.example/config",
    "https://ha.example:8443/config",
])
def test_rest_refuses_redirect_outside_configured_origin(url):
    rest = Rest("https://ha.example", "synthetic")
    handler = next(item for item in rest._opener.handlers if isinstance(item, _OriginRedirect))
    req = Request("https://ha.example/api/config", headers={"Authorization": "Bearer synthetic"})
    with pytest.raises(FetchError, match="leave the configured origin"):
        handler.redirect_request(req, None, 302, "", {}, url)


def test_rest_redirect_with_equivalent_explicit_https_port_keeps_authentication():
    rest = Rest("https://ha.example", "synthetic")
    handler = next(item for item in rest._opener.handlers if isinstance(item, _OriginRedirect))
    req = Request("https://ha.example/api/config", headers={"Authorization": "Bearer synthetic"})
    redirected = handler.redirect_request(req, None, 302, "", {}, "https://ha.example:443/config")
    assert redirected.get_header("Authorization") == "Bearer synthetic"
