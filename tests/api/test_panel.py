from bot.tickets.panel import PanelView
from tests.api.helpers import Env, make_text_channel


def options(view: PanelView) -> list[str]:
    select = view.children[0]
    return [option.label for option in select.options]  # type: ignore[attr-defined]


async def test_publish_then_edit(env: Env) -> None:
    channel = make_text_channel(500)
    env.guild.channels.append(channel)
    await env.login(admin=True)
    await env.post("/categories", {"name": "帳務"})
    await env.post("/categories", {"name": "停用", "enabled": False})

    response = await env.post("/panel/publish", {"channel_id": "500"})
    assert response.status_code == 200
    assert response.json() == {"channel_id": "500", "message_id": "5000"}
    assert options(channel.send.call_args.kwargs["view"]) == ["帳務"]

    # 同一個頻道再發布一次：編輯原本的訊息
    await env.post("/categories", {"name": "技術"})
    response = await env.post("/panel/publish", {"channel_id": "500"})
    assert response.json()["message_id"] == "5000"
    channel.send.assert_awaited_once()
    assert options(channel.partial.edit.call_args.kwargs["view"]) == ["帳務", "技術"]

    settings = (await env.client.get("/settings")).json()
    assert (settings["panel_channel_id"], settings["panel_message_id"]) == ("500", "5000")


async def test_move_to_another_channel(env: Env) -> None:
    old, new = make_text_channel(500), make_text_channel(501)
    env.guild.channels += [old, new]
    await env.login(admin=True)
    await env.post("/panel/publish", {"channel_id": "500"})

    response = await env.post("/panel/publish", {"channel_id": "501"})
    assert response.json() == {"channel_id": "501", "message_id": "5010"}
    old.partial.delete.assert_awaited_once()


async def test_without_categories_select_is_disabled(env: Env) -> None:
    channel = make_text_channel(500)
    env.guild.channels.append(channel)
    await env.login(admin=True)
    await env.post("/panel/publish", {"channel_id": "500"})
    view = channel.send.call_args.kwargs["view"]
    assert view.children[0].disabled


async def test_validation(env: Env) -> None:
    env.guild.channels.append(make_text_channel(500))
    await env.login(admin=True)
    assert (await env.post("/panel/publish", {"channel_id": "999"})).status_code == 422
    for i in range(26):
        await env.post("/categories", {"name": f"分類 {i}"})
    assert (await env.post("/panel/publish", {"channel_id": "500"})).status_code == 422
