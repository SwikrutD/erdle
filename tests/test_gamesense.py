import json

import pytest

from erdle.canvas import Canvas
from erdle.gamesense import (
    DEVICE_TYPE,
    HEARTBEAT_INTERVAL,
    CoreProps,
    GameSenseClient,
    GameSenseError,
    RecordingTransport,
    read_core_props,
)


@pytest.fixture
def transport():
    return RecordingTransport()


@pytest.fixture
def client(transport):
    return GameSenseClient(CoreProps("127.0.0.1:51248"), transport=transport)


# --- coreProps discovery ---------------------------------------------------


def test_reads_address_from_core_props(tmp_path):
    path = tmp_path / "coreProps.json"
    path.write_text(json.dumps({"address": "127.0.0.1:51248"}), encoding="utf-8")
    assert read_core_props(path).address == "127.0.0.1:51248"


def test_missing_core_props_is_a_clear_error(tmp_path):
    with pytest.raises(GameSenseError, match="Is SteelSeries GG running"):
        read_core_props(tmp_path / "nope.json")


def test_malformed_core_props_rejected(tmp_path):
    path = tmp_path / "coreProps.json"
    path.write_text("{{{", encoding="utf-8")
    with pytest.raises(GameSenseError, match="invalid JSON"):
        read_core_props(path)


def test_core_props_without_address_rejected(tmp_path):
    path = tmp_path / "coreProps.json"
    path.write_text(json.dumps({"other": 1}), encoding="utf-8")
    with pytest.raises(GameSenseError, match="no 'address'"):
        read_core_props(path)


def test_base_url_is_http():
    assert CoreProps("127.0.0.1:1234").base_url == "http://127.0.0.1:1234"


# --- registration ----------------------------------------------------------


def test_register_posts_metadata_then_binding(client, transport):
    client.register()
    assert transport.urls() == [
        "http://127.0.0.1:51248/game_metadata",
        "http://127.0.0.1:51248/bind_game_event",
    ]


def test_metadata_sets_a_deinitialise_timer(client, transport):
    client.register()
    payload = transport.calls[0][1]
    assert payload["game"] == "ERDLE"
    assert payload["deinitialize_timer_length_ms"] > 0


def test_binding_declares_the_right_device(client, transport):
    client.register()
    handler = transport.calls[1][1]["handlers"][0]
    assert handler["device-type"] == DEVICE_TYPE
    assert handler["mode"] == "screen"
    assert handler["datas"][0]["has-text"] is False
    assert len(handler["datas"][0]["image-data"]) == 640


# --- frames ----------------------------------------------------------------


def test_send_bitmap_posts_a_game_event(client, transport):
    client.send_bitmap(Canvas().pack())
    url, payload = transport.calls[-1]
    assert url.endswith("/game_event")
    assert payload["game"] == "ERDLE"
    assert len(payload["data"]["frame"]["image-data-128x40"]) == 640


def test_send_bitmap_rejects_wrong_size(client):
    with pytest.raises(ValueError, match="640"):
        client.send_bitmap([0] * 100)


def test_rendered_canvas_survives_the_round_trip(client, transport):
    canvas = Canvas()
    canvas.draw_text("MALENIA", 4, 4)
    client.send_bitmap(canvas.pack())
    sent = transport.calls[-1][1]["data"]["frame"]["image-data-128x40"]
    assert Canvas.from_packed(sent).to_rows() == canvas.to_rows()


def test_payload_is_json_serialisable(client, transport):
    canvas = Canvas()
    canvas.draw_text("TEST", 0, 0)
    client.send_bitmap(canvas.pack())
    json.dumps(transport.calls[-1][1])  # must not raise


# --- heartbeat -------------------------------------------------------------


def test_first_heartbeat_is_sent(transport):
    client = GameSenseClient(CoreProps("h:1"), transport=transport, clock=lambda: 0.0)
    assert client.heartbeat() is True


def test_heartbeat_is_throttled(transport):
    now = [0.0]
    client = GameSenseClient(
        CoreProps("h:1"), transport=transport, clock=lambda: now[0]
    )
    client.heartbeat()
    now[0] = HEARTBEAT_INTERVAL / 2
    assert client.heartbeat() is False
    now[0] = HEARTBEAT_INTERVAL + 1
    assert client.heartbeat() is True


def test_forced_heartbeat_ignores_throttle(transport):
    client = GameSenseClient(CoreProps("h:1"), transport=transport, clock=lambda: 0.0)
    client.heartbeat()
    assert client.heartbeat(force=True) is True


def test_heartbeat_payload_names_the_game(client, transport):
    client.heartbeat()
    assert transport.calls[-1][1] == {"game": "ERDLE"}


def test_remove_game_posts(client, transport):
    client.remove_game()
    assert transport.urls()[-1].endswith("/remove_game")


# --- transport contract ----------------------------------------------------


def test_recording_transport_captures_everything(client, transport):
    client.register()
    client.send_bitmap(Canvas().pack())
    client.heartbeat()
    assert len(transport.calls) == 4
    assert transport.last_payload() == {"game": "ERDLE"}
