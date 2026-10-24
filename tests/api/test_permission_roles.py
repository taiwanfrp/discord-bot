from tests.api.helpers import ADMIN_ROLE_ID, Env, make_role


async def test_replace_and_get(env: Env) -> None:
    env.guild.roles += [make_role(20), make_role(21)]
    await env.login(admin=True)
    billing = (await env.post("/categories", {"name": "帳務"})).json()
    tech = (await env.post("/categories", {"name": "技術"})).json()

    response = await env.put(
        "/permission-roles",
        {
            "admin_role_ids": [str(ADMIN_ROLE_ID), str(ADMIN_ROLE_ID)],
            "category_roles": [{"category_id": billing["id"], "role_ids": ["20", "21", "20"]}],
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "admin_role_ids": [str(ADMIN_ROLE_ID)],
        "category_roles": [
            {"category_id": billing["id"], "role_ids": ["20", "21"]},
            {"category_id": tech["id"], "role_ids": []},
        ],
    }
    assert (await env.client.get("/permission-roles")).json() == response.json()


async def test_category_role_gives_access(env: Env) -> None:
    env.guild.roles.append(make_role(20))
    await env.login(admin=True)
    billing = (await env.post("/categories", {"name": "帳務"})).json()
    await env.put(
        "/permission-roles",
        {
            "admin_role_ids": [str(ADMIN_ROLE_ID)],
            "category_roles": [{"category_id": billing["id"], "role_ids": ["20"]}],
        },
    )

    await env.login(role_ids=[20])
    me = (await env.client.get("/me")).json()
    assert me["is_admin"] is False
    assert me["category_ids"] == [billing["id"]]


async def test_validation(env: Env) -> None:
    await env.login(admin=True)
    for body in (
        {"admin_role_ids": ["999"], "category_roles": []},
        {"admin_role_ids": [], "category_roles": [{"category_id": 999, "role_ids": []}]},
    ):
        assert (await env.put("/permission-roles", body)).status_code == 422, body
