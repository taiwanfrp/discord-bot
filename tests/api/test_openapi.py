from tests.api.helpers import Env


async def test_every_route_has_a_tag(env: Env) -> None:
    """/docs 依照 tag 分組；沒有 tag 的會被放進 default。"""
    schema = (await env.client.get("/openapi.json")).json()
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            assert operation.get("tags"), f"{method.upper()} {path} 沒有 tag"
