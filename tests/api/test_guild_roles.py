from tests.api.helpers import ADMIN_ROLE_ID, Env, make_role


async def test_create_role(env: Env) -> None:
    await env.login(admin=True)
    response = await env.post("/guild/roles", {"name": "客服-帳務", "color": 0x00FF00})
    assert response.status_code == 201
    assert response.json()["name"] == "客服-帳務"
    kwargs = env.guild.create_role.call_args.kwargs
    assert kwargs["colour"].value == 0x00FF00
    assert "從網頁操作" in kwargs["reason"]


async def test_cannot_edit_role_above_bot(env: Env) -> None:
    env.guild.roles.append(make_role(30, assignable=False))
    await env.login(admin=True)
    assert (await env.patch("/guild/roles/30", {"name": "x"})).status_code == 403
    assert (await env.delete("/guild/roles/30")).status_code == 403
    assert (await env.patch("/guild/roles/999", {"name": "x"})).status_code == 422


async def test_delete_role_removes_permission(env: Env) -> None:
    role = make_role(20)
    env.guild.roles.append(role)
    await env.login(admin=True)
    await env.put(
        "/permission-roles", {"admin_role_ids": [str(ADMIN_ROLE_ID), "20"], "category_roles": []}
    )

    assert (await env.delete("/guild/roles/20")).status_code == 204
    role.delete.assert_awaited_once()
    roles = (await env.client.get("/permission-roles")).json()
    assert roles["admin_role_ids"] == [str(ADMIN_ROLE_ID)]
