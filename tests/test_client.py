import httpx
import pytest
import respx

from nhlscreen.nhl.client import NHLClient, NHLUnavailable

BASE = "https://api-web.nhle.com/v1"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def client(clock):
    c = NHLClient(BASE, clock=clock)
    yield c
    await c.aclose()


@respx.mock
async def test_respects_cache_control_max_age(client, clock):
    route = respx.get(f"{BASE}/standings/now").mock(
        return_value=httpx.Response(200, json={"v": 1}, headers={"Cache-Control": "max-age=19"})
    )
    assert await client.standings() == {"v": 1}
    clock.t += 10
    await client.standings()
    assert route.call_count == 1  # still fresh at the CDN, no refetch
    clock.t += 10
    await client.standings()
    assert route.call_count == 2


@respx.mock
async def test_rate_limit_serves_stale_data_and_backs_off(client, clock):
    route = respx.get(f"{BASE}/gamecenter/1/play-by-play").mock(
        side_effect=[
            httpx.Response(200, json={"v": 1}),
            httpx.Response(429, headers={"Retry-After": "60"}),
            httpx.Response(200, json={"v": 2}),
        ]
    )
    assert await client.play_by_play(1) == {"v": 1}
    assert await client.play_by_play(1) == {"v": 1}  # 429 -> last good data
    assert client.backoff_remaining == 60

    clock.t += 30
    assert await client.play_by_play(1) == {"v": 1}
    assert route.call_count == 2  # no request while backing off

    clock.t += 31
    assert await client.play_by_play(1) == {"v": 2}
    assert client.backoff_remaining == 0


@respx.mock
async def test_outage_without_cache_raises(client):
    respx.get(f"{BASE}/standings/now").mock(return_value=httpx.Response(503))
    with pytest.raises(NHLUnavailable):
        await client.standings()


@respx.mock
async def test_network_error_backs_off_exponentially(client, clock):
    respx.get(f"{BASE}/standings/now").mock(side_effect=httpx.ConnectError("down"))
    for expected in (2, 4, 8):
        with pytest.raises(NHLUnavailable):
            await client.standings()
        assert client.backoff_remaining == expected
        clock.t += expected


@respx.mock
async def test_404_does_not_trigger_backoff(client):
    respx.get(f"{BASE}/player/1/landing").mock(return_value=httpx.Response(404))
    with pytest.raises(NHLUnavailable):
        await client.player(1)
    assert client.backoff_remaining == 0
