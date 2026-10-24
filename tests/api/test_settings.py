from tests.api.helpers import Env, make_category_channel, make_text_channel


async def test_admin_only(env: Env) -> None:
    await env.login()
    assert (await env.client.get("/settings")).status_code == 403
    assert (await env.patch("/settings", {"max_open_tickets_per_user": 5})).status_code == 403


async def test_defaults(env: Env) -> None:
    await env.login(admin=True)
    response = await env.client.get("/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["channel_name_template"] == "客服單-{id}"
    assert body["max_open_tickets_per_user"] == 3
    assert body["inactivity_remind_after_hours"] == 96
    assert body["inactivity_close_after_hours"] == 24
    assert body["max_attachment_size"] == 1024**3
    assert body["allowed_file_types"] is None


async def test_patch_only_changes_sent_fields(env: Env) -> None:
    env.guild.channels += [make_text_channel(500), make_category_channel(600)]
    await env.login(admin=True)

    response = await env.patch(
        "/settings",
        {
            "log_channel_id": "500",
            "default_discord_category_id": "600",
            "inactivity_close_after_hours": None,
            "allowed_file_types": ["PNG", "jpg", " png "],
        },
    )
    assert response.status_code == 200, response.json()
    body = (await env.client.get("/settings")).json()
    assert body["log_channel_id"] == "500"
    assert body["default_discord_category_id"] == "600"
    assert body["inactivity_close_after_hours"] is None
    assert body["inactivity_remind_after_hours"] == 96
    assert body["allowed_file_types"] == ["jpg", "png"]


async def test_patch_validation(env: Env) -> None:
    env.guild.channels += [make_text_channel(500), make_category_channel(600)]
    await env.login(admin=True)

    for body in (
        {"channel_name_template": "{name}"},
        {"channel_name_template": None},
        {"max_open_tickets_per_user": 0},
        {"max_attachment_size": 1024**3 + 1},
        {"allowed_file_types": [".png"]},
        # 頻道不存在，或類型不對
        {"log_channel_id": "999"},
        {"log_channel_id": "600"},
        {"default_discord_category_id": "500"},
    ):
        response = await env.patch("/settings", body)
        assert response.status_code == 422, body
