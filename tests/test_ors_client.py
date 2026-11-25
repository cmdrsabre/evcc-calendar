"""OpenRouteService client: URLs (the old host api.openrouteservice.org answers HTTP 410)."""
import pytest

from evccplan import clients
from evccplan.clients import ApiError, OrsClient


def test_ors_client_uses_the_heigit_host(monkeypatch):
    urls = []
    monkeypatch.setattr(clients, "_request", lambda m, url, h, body=None, timeout=20.0: (urls.append(url), (200, {"features": []}))[1])
    o = OrsClient("k")
    o.geocode("x")
    with pytest.raises(ApiError):
        o.route((52.5, 13.4), (52.4, 13.3))
    assert urls[0].startswith("https://api.heigit.org/pelias/v1/search?")
    assert urls[1].startswith("https://api.heigit.org/openrouteservice/v2/directions/driving-car?")
