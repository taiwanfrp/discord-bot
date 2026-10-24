from bot.db.models import Source, Ticket
from tests.api.helpers import USER_ID, Env, make_category_channel


async def test_crud(env: Env) -> None:
    env.guild.channels.append(make_category_channel(600))
    await env.login(admin=True)

    response = await env.post("/categories", {"name": "帳務", "position": 2})
    assert response.status_code == 201
    second = response.json()
    response = await env.post(
        "/categories", {"name": "技術", "position": 1, "emoji": "🛠️", "discord_category_id": "600"}
    )
    first = response.json()
    assert first["discord_category_id"] == "600"

    names = [c["name"] for c in (await env.client.get("/categories")).json()]
    assert names == ["技術", "帳務"]

    response = await env.patch(f"/categories/{second['id']}", {"enabled": False})
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["name"] == "帳務"

    assert (await env.delete(f"/categories/{second['id']}")).status_code == 204
    assert (await env.patch(f"/categories/{second['id']}", {"name": "x"})).status_code == 404


async def test_non_admin_sees_only_enabled(env: Env) -> None:
    await env.login(admin=True)
    await env.post("/categories", {"name": "啟用"})
    await env.post("/categories", {"name": "停用", "enabled": False})

    await env.login()
    assert (await env.post("/categories", {"name": "x"})).status_code == 403
    names = [c["name"] for c in (await env.client.get("/categories")).json()]
    assert names == ["啟用"]


async def test_validation(env: Env) -> None:
    await env.login(admin=True)
    for body in (
        {"name": ""},
        {"name": "x", "discord_category_id": "999"},
        # 這個伺服器沒有的自訂表情
        {"name": "x", "emoji": "<:custom:123456789012345678>"},
    ):
        assert (await env.post("/categories", body)).status_code == 422, body
    category = (await env.post("/categories", {"name": "x"})).json()
    assert (await env.patch(f"/categories/{category['id']}", {"name": None})).status_code == 422


async def test_cannot_delete_category_with_tickets(env: Env) -> None:
    await env.login(admin=True)
    category = (await env.post("/categories", {"name": "帳務"})).json()
    async with env.sessionmaker() as session:
        session.add(
            Ticket(
                category_id=category["id"],
                source=Source.WEB,
                opener_id=USER_ID,
                subject="主旨",
                content="內容",
            )
        )
        await session.commit()

    assert (await env.delete(f"/categories/{category['id']}")).status_code == 409
