"""
Веб-сервер для MCP-моста: статическая страница чата + SSE-эндпоинт,
стримящий события (конфигурация, вызовы инструментов, финальный ответ) в
браузер по мере их появления — то же самое, что раньше печаталось в
терминал построчно.

Запуск (из этой же папки, venv с requirements.txt уже активен):
    uvicorn server:app --host 0.0.0.0 --port 8000
"""

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from bridge_core import McpBridge

bridge = McpBridge()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bridge.connect()
    print(f"[server] Подключено к MCP, инструментов доступно модели: {len(bridge.tools_info)} из {bridge.config_info['tools_total']}")
    print(f"[server] LLM-hub: {bridge.config_info['llm_hub_base_url']}")
    print(f"[server] Модель: {bridge.config_info['inference_model']}")
    yield
    await bridge.close()


app = FastAPI(title="MCP-мост OpenMetadata", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/api/config")
async def config():
    return bridge.config_info


@app.get("/api/tools")
async def tools():
    return {"tools": bridge.tools_info}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get("/api/ask")
async def ask(q: str = Query(..., min_length=1, max_length=2000)):
    async def gen():
        async for event in bridge.ask(q):
            yield _sse(event)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
