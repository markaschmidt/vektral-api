"""Frozen voice envelope and the pane CRUD reference actions."""

from app.schemas import TurnActionPageEdit, TurnActionPaneClose, TurnActionPaneOpen
from app.services.voice_actions import (
    CHEAP_ACTION_TYPES,
    QUEUED_ACTION_TYPES,
    close_pane_result,
    merge_planned_actions,
    normalize_turn_id,
    open_screen_result,
    update_pane_result,
)


def test_turn_id_envelope():
    assert normalize_turn_id("", required=True)[1]["error"] == "missing_turn_id"
    assert normalize_turn_id("short", required=True)[1]["error"] == "invalid_turn_id"
    turn_id, err = normalize_turn_id("turn_ok_01", required=True)
    assert err is None
    assert turn_id == "turn_ok_01"
    assert normalize_turn_id("", required=False) == ("", None)


def test_open_screen_result_is_the_paint_contract():
    payload = open_screen_result(
        [{"id": "p1", "title": "Screen", "route": "/screen-a", "created": True}]
    )
    assert payload["action"] == "open_screen"
    assert payload["panes"] == [
        {"id": "p1", "title": "Screen", "route": "/screen-a", "created": True}
    ]
    assert payload["ok"] is True


def test_screen_command_keeps_cheap_pane_open_when_the_model_edits():
    assert "pane.open" in CHEAP_ACTION_TYPES
    assert "page.edit" in QUEUED_ACTION_TYPES
    deterministic = [
        TurnActionPaneOpen(
            type="pane.open",
            action_id="a1",
            title="Screen",
            route="",
            force_new=True,
        )
    ]
    planned = [TurnActionPageEdit(type="page.edit", action_id="a9", instruction="green")]
    merged = merge_planned_actions(
        screen_command=True,
        deterministic=deterministic,
        planned=planned,
    )
    assert [action.type for action in merged] == ["pane.open"]
    assert merged[0] is deterministic[0]


def test_pane_crud_results_are_typed():
    assert "pane.update" in CHEAP_ACTION_TYPES
    assert "pane.close" in CHEAP_ACTION_TYPES
    closed = close_pane_result("pane_1", "Shop")
    assert closed["action"] == "close_pane"
    assert closed["panes"][0]["id"] == "pane_1"
    updated = update_pane_result({"id": "pane_2", "title": "Shop", "route": "/shop"})
    assert updated["action"] == "update_pane"
    assert updated["panes"][0]["route"] == "/shop"


def test_close_command_survives_model_page_edit():
    deterministic = [
        TurnActionPaneClose(
            type="pane.close", action_id="a1", pane_id="pane_1", title="Shop"
        )
    ]
    planned = [TurnActionPageEdit(type="page.edit", action_id="a9", instruction="green")]
    merged = merge_planned_actions(
        screen_command=True, deterministic=deterministic, planned=planned
    )
    assert [action.type for action in merged] == ["pane.close"]


def test_place_the_shop_pane_resolves_shop():
    from app.services import panes as panes_svc

    assert panes_svc.is_screen_command("place the pane")
    assert panes_svc.is_screen_command("place the shop page pane")
    assert panes_svc._named_screen_token("place the shop page pane") == "shop"  # noqa: SLF001
    assert panes_svc.is_pane_close_command("remove this pane")
    assert panes_svc.is_pane_close_command("delete the shop pane")
    assert not panes_svc.is_pane_close_command("place the pane")
    assert panes_svc.is_pane_update_command("rename this screen to Shop")
    assert panes_svc.is_pane_update_command("point this screen at /shop")
    assert panes_svc.parse_pane_update("rename this screen to Shop")["title"] == "Shop"
    assert panes_svc.parse_pane_update("point this screen at /shop")["route"] == "/shop"
    assert panes_svc.is_blank_screen_request("place a blank screen")
    assert panes_svc.parse_screen_targets("place a blank screen") == [
        {"title": "Blank", "route": "", "kind": "blank", "force_new": "1"}
    ]
    turned = panes_svc.parse_pane_update("turn this screen into the shop page")
    assert turned["kind"] == "preview"
    assert turned["route"] == "/shop"
    assert panes_svc.parse_pane_update("turn this screen into a chat")["kind"] == "chat"