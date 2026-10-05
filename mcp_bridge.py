"""
MCP-мост: LLM-hub (OpenAI-совместимый шлюз) <-> OpenMetadata (MCP-сервер каталога).

ПЕРЕНОСИМОСТЬ
Все параметры вынесены в переменные окружения (см. .env.example рядом).
При переносе проекта на другую ВМ/среду (например, dev) потребуется
перенастроить только блоки, отмеченные ниже как "ТОЧКА СОПРЯЖЕНИЯ" —
остальной код трогать не нужно.

Всего две точки сопряжения, которые меняются при смене среды:
  1) LLM-HUB         — адрес и ключ OpenAI-совместимого шлюза.
  2) INFERENCE MODEL — имя модели (алиас), которую должен использовать
                        этот шлюз для инференса.

Подключение к каталогу OpenMetadata (MCP-сервер) — отдельная, третья
переменная часть (OPENMETADATA_MCP_URL / OPENMETADATA_TOKEN). Она не
связана с LLM-hub и меняется только если переносится сам каталог, а не
LLM-инфраструктура.
"""

import asyncio
import json
import os

import httpx as httpx2

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client as streamablehttp_client
from openai import OpenAI

# ---------------------------------------------------------------------------
# OpenMetadata MCP-сервер (источник каталога данных).
# Меняется, если переносится/меняется сам стенд OpenMetadata — к LLM-hub и
# инференсу отношения не имеет.
# ---------------------------------------------------------------------------
OM_BASE = os.environ["OPENMETADATA_MCP_URL"]  # напр. http://10.0.0.7:8585/mcp
OM_TOKEN = os.environ["OPENMETADATA_TOKEN"]  # JWT бота, OpenMetadata -> Settings -> Bots

# ---------------------------------------------------------------------------
# >>> ТОЧКА СОПРЯЖЕНИЯ 1: LLM-HUB <<<
# OpenAI-совместимый шлюз, который реально обслуживает запросы на инференс
# и function calling.
#
# Сейчас (прод, cloud.ru): self-hosted LiteLLM на той же ВМ
#   LLM_HUB_BASE_URL=http://127.0.0.1:4000/v1
#   LLM_HUB_API_KEY=<LITELLM_MASTER_KEY или virtual key>
#
# В dev-среде здесь может оказаться ДРУГОЙ LiteLLM-инстанс (другой хост,
# другой ключ) или вообще другой OpenAI-совместимый шлюз. Код ниже этого
# не замечает — достаточно поменять эти две переменные окружения.
# Шлюз обязан поддерживать OpenAI Chat Completions API с tools (function
# calling); без этого MCP-инструменты вызываться не будут.
# ---------------------------------------------------------------------------
LLM_HUB_BASE_URL = os.environ["LLM_HUB_BASE_URL"]
LLM_HUB_API_KEY = os.environ["LLM_HUB_API_KEY"]

# ---------------------------------------------------------------------------
# >>> ТОЧКА СОПРЯЖЕНИЯ 2: INFERENCE MODEL <<<
# Имя модели ровно так, как оно заведено (алиас) на LLM-hub выше — не
# обязательно совпадает с именем модели у вендора.
#
# Сейчас (прод): "deepseek-v3.2" — алиас в LiteLLM, который проксирует
# на cloud.ru Foundation Models.
#
# В dev-среде здесь может быть другой алиас — своя ML-модель, заведённая
# в LiteLLM/шлюзе под собственным именем. Проверяйте, что модель
# поддерживает tool calling (см. примечание выше).
# ---------------------------------------------------------------------------
INFERENCE_MODEL = os.environ.get("INFERENCE_MODEL", "deepseek-v3.2")

# Прочие параметры — не являются точками сопряжения, можно не трогать.
MAX_STEPS = int(os.environ.get("MCP_BRIDGE_MAX_STEPS", "20"))
TOOL_RESULT_PREVIEW_CHARS = int(os.environ.get("MCP_BRIDGE_PREVIEW_CHARS", "800"))

SYSTEM_PROMPT = (
    "Ты помощник по данным OpenMetadata. Используй инструменты, "
    "чтобы ответить на вопрос пользователя. "
    "СТРОГОЕ ПРАВИЛО: никогда не вызывай один и тот же инструмент "
    "с теми же или почти теми же аргументами повторно — если "
    "результат уже получен, используй его. "
    "Инструмент search_metadata поддерживает параметр entityType "
    "(например: table, container, dbservice, pipeline, database, "
    "dashboard). Если вопрос касается структуры каталога — какие "
    "есть сервисы, таблицы, контейнеры и т.п. — сразу используй "
    "entityType-фильтр вместо свободного текстового поиска по "
    "ключевым словам: это находит результат с первого раза и "
    "экономит шаги. Свободный текстовый поиск оставляй только "
    "для вопросов о содержании (описания, теги, значения). "
    "Как только у тебя есть достаточно информации (даже неполной) "
    "для ответа — сразу дай финальный текстовый ответ без вызова "
    "новых инструментов, не пытайся собрать исчерпывающие данные. "
    f"После 4-5 вызовов инструментов в любом случае формулируй "
    "финальный ответ на основе того, что уже собрано."
)


def mcp_tool_to_openai_schema(tool):
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.input_schema or {"type": "object", "properties": {}},
        },
    }


async def main():
    print(f"[config] OpenMetadata MCP: {OM_BASE}")
    print(f"[config] LLM-hub:          {LLM_HUB_BASE_URL}")
    print(f"[config] Inference model:  {INFERENCE_MODEL}")

    question = input("Вопрос: ")

    om_http_client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {OM_TOKEN}"})

    async with streamablehttp_client(OM_BASE, http_client=om_http_client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tools = tools_result.tools
            print(f"[MCP] Доступно инструментов: {len(tools)}")
            for t in tools:
                print(f"  - {t.name}: {t.description}")

            openai_tools = [mcp_tool_to_openai_schema(t) for t in tools]

            client = OpenAI(base_url=LLM_HUB_BASE_URL, api_key=LLM_HUB_API_KEY)

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ]

            for step in range(MAX_STEPS):
                resp = client.chat.completions.create(
                    model=INFERENCE_MODEL,
                    messages=messages,
                    tools=openai_tools,
                )
                msg = resp.choices[0].message

                if not msg.tool_calls:
                    print("\n=== Ответ модели ===")
                    print(msg.content)
                    return

                messages.append(msg.model_dump(exclude_none=True))

                for tool_call in msg.tool_calls:
                    name = tool_call.function.name
                    try:
                        args = json.loads(tool_call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    print(f"[MCP] Вызов инструмента: {name}({args})")

                    result = await session.call_tool(name, args)
                    result_text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
                    preview = result_text[:TOOL_RESULT_PREVIEW_CHARS]
                    print(f"[MCP] Результат ({len(result_text)} симв.): {preview}")

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result_text,
                        }
                    )

            print("Достигнут лимит шагов без финального ответа.")


if __name__ == "__main__":
    asyncio.run(main())
