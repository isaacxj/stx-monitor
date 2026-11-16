from types import SimpleNamespace

import httpx

from monitor.jellyfin import Jellyfin, parse_session

MOVIE = {"Id": "s1", "UserName": "isaac", "DeviceName": "Toshiba Fire TV", "Client": "Netflixx",
         "NowPlayingItem": {"Name": "The Lighthouse Keeper", "Type": "Movie", "MediaType": "Video", "ProductionYear": 2023,
                            "RunTimeTicks": 67_200_000_000},
         "PlayState": {"PositionTicks": 6_000_000_000, "IsPaused": False, "PlayMethod": "DirectPlay"}}


def episode(hardware):
    return {"Id": "s2", "UserName": "guest", "DeviceName": "iPhone", "Client": "Jellyfin iOS",
            "NowPlayingItem": {"Name": "Low Tide", "Type": "Episode", "MediaType": "Video", "SeriesName": "Harbor Lights",
                               "ParentIndexNumber": 2, "IndexNumber": 5, "RunTimeTicks": 26_400_000_000},
            "PlayState": {"PositionTicks": 0, "IsPaused": True, "PlayMethod": "Transcode"},
            "TranscodingInfo": {"IsVideoDirect": False, "VideoCodec": "h264", "Bitrate": 4_000_000,
                                "HardwareAccelerationType": hardware, "TranscodeReasons": ["ContainerBitrateExceedsLimit"]}}


def test_parse_sessions():
    assert parse_session({"Id": "idle", "UserName": "isaac"}) is None
    m = parse_session(MOVIE)
    assert (m["title"], m["subtitle"], m["mode"], m["label"]) == ("The Lighthouse Keeper", "2023", "direct", "Direct play")
    assert m["position"] == 600 and m["runtime"] == 6720
    e = parse_session(episode("qsv"))
    assert (e["title"], e["subtitle"], e["paused"]) == ("Harbor Lights S02E05", "Low Tide", True)
    assert (e["mode"], e["label"]) == ("hardware", "Transcoding on Quick Sync")
    assert parse_session(episode("none"))["label"] == "Transcoding on the CPU"
    assert parse_session(episode(None))["mode"] == "software"
    remux = {**MOVIE, "TranscodingInfo": {"IsVideoDirect": True, "IsAudioDirect": False}}
    assert parse_session(remux)["label"] == "Direct stream"


def setup(handler):
    events, broadcasts = [], []
    col = SimpleNamespace(add_event=lambda ev: events.append(ev) or ev, broadcast=lambda kind, data: broadcasts.append((kind, data)))
    return Jellyfin(col, "", "secret-key", transport=httpx.MockTransport(handler)), events, broadcasts


async def test_poll_sends_the_key_and_reports_cpu_transcodes_once():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[MOVIE, episode("none"), {"Id": "idle"}])

    j, events, broadcasts = setup(handler)
    await j.poll()
    assert 'Token="secret-key"' in seen[0].headers["authorization"] and seen[0].url.path == "/Sessions"
    assert str(seen[0].url).startswith("http://127.0.0.1:8096/")
    assert [s["mode"] for s in j.view()["sessions"]] == ["direct", "software"]
    assert [e["title"] for e in events] == ["Jellyfin is transcoding on the CPU"]
    await j.poll()
    assert len(events) == 1 and len(broadcasts) == 1  # nothing changed: no repeat event, no repeat broadcast
    await j.close()


async def test_poll_explains_a_bad_key_and_an_unreachable_server():
    j, events, _ = setup(lambda request: httpx.Response(401))
    await j.poll()
    assert j.view()["ok"] is False and "API key" in j.view()["error"] and not events
    await j.close()

    def down(request):
        raise httpx.ConnectError("refused")

    j, *_ = setup(down)
    await j.poll()
    assert j.view()["error"] == "Jellyfin isn't answering"
    await j.close()


async def test_off_without_a_key():
    j = Jellyfin(SimpleNamespace(), "", "")
    assert not j.configured and j.view() == {"configured": False, "ok": None, "error": None, "sessions": []}
    await j.poll()
    await j.close()
