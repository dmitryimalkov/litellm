"""
Общая логика MCP-моста, вынесенная из mcp_bridge.py в переиспользуемый класс
без input()/print() — чтобы один и тот же код обслуживал и CLI, и веб-сервер.

Те же три группы переменных окружения, что и в mcp_bridge.py — см. .env.example:
  - OPENMETADATA_MCP_URL / OPENMETADATA_TOKEN — каталог (не точка сопряжения с LLM)
  - ТОЧКА СОПРЯЖЕНИЯ 1: LLM_HUB_BASE_URL / LLM_HUB_API_KEY — LLM-hub
  - ТОЧКА СОПРЯЖЕНИЯ 2: INFERENCE_MODEL — модель на этом LLM-hub
"""

import asyncio
import json
import os
import time
from contextlib import AsyncExitStack
from typing import AsyncIterator, Optional

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client as streamablehttp_client
import openai
from openai import OpenAI

OM_BASE = os.environ["OPENMETADATA_MCP_URL"]
OM_TOKEN = os.environ["OPENMETADATA_TOKEN"]

# >>> ТОЧКА СОПРЯЖЕНИЯ 1: LLM-HUB <<< (см. комментарий в mcp_bridge.py)
LLM_HUB_BASE_URL = os.environ["LLM_HUB_BASE_URL"]
LLM_HUB_API_KEY = os.environ["LLM_HUB_API_KEY"]

# >>> ТОЧКА СОПРЯЖЕНИЯ 2: INFERENCE MODEL <<< (см. комментарий в mcp_bridge.py)
INFERENCE_MODEL = os.environ.get("INFERENCE_MODEL", "deepseek-v3.2")

MAX_STEPS = int(os.environ.get("MCP_BRIDGE_MAX_STEPS", "20"))
# Сколько раз просить модель повторить, если она написала вызов инструмента
# обычным текстом вместо настоящего tool_calls (случается у DeepSeek через шлюз).
PSEUDO_TOOL_RETRIES = int(os.environ.get("MCP_BRIDGE_PSEUDO_RETRIES", "2"))
# Низкая температура уменьшает такие сбои. Пустое значение = не передавать параметр.
_TEMP = os.environ.get("MCP_BRIDGE_TEMPERATURE", "0.2").strip()
TEMPERATURE = float(_TEMP) if _TEMP else None
TOOL_RESULT_PREVIEW_CHARS = int(os.environ.get("MCP_BRIDGE_PREVIEW_CHARS", "800"))

# Таймаут одного вызова модели, секунд. Без него зависший запрос к LLM-hub ждёт
# десять минут (таймаут клиента OpenAI по умолчанию), а страница показывает «думает…».
LLM_TIMEOUT = float(os.environ.get("MCP_BRIDGE_LLM_TIMEOUT", "120"))

# Белый список MCP-инструментов, которые видит модель. Это и безопасность (по умолчанию
# модели НЕ доступны create_entity / patch_entity / create_lineage / create_test_case —
# она не может менять каталог), и экономия: описание каждого инструмента уходит в
# модель при КАЖДОМ вызове (все 15 инструментов давали ~10 000 токенов входа на шаг).
# "*" — отдать все инструменты сервера (например, в dev).
_ALLOW = os.environ.get(
    "MCP_BRIDGE_TOOLS_ALLOW",
    "search_metadata,semantic_search,get_entity_details,get_entity_lineage,describe_entity_type",
).strip()
TOOLS_ALLOW = None if _ALLOW == "*" else {t.strip() for t in _ALLOW.split(",") if t.strip()}

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
    "После 4-5 вызовов инструментов в любом случае формулируй "
    "финальный ответ на основе того, что уже собрано. "
    "Если спрашивают, какие источники данных или сервисы подключены, "
    "проверь четыре основных типа сервисов: databaseService, "
    "dashboardService, pipelineService, storageService, и перечисли "
    "найденные по каждому типу. Другие типы (messaging, mlmodel, api, "
    "search, metadata) проверяй только если о них спросили явно. "
    "Если спрашивают о происхождении данных (lineage — откуда данные "
    "приходят и куда уходят), найди сущность через search_metadata и "
    "вызови get_entity_lineage с её fullyQualifiedName. Схема связей "
    "строится на странице автоматически по результату этого инструмента — "
    "НЕ рисуй схему символами (ASCII, стрелки, блоки кода) и не выдумывай связи; опиши словами или таблицей, что нашёл, "
    "а если связей нет — так и скажи. "
    "Вызывай инструменты ТОЛЬКО через механизм function calling. "
    "Никогда не пиши вызов инструмента или JSON с аргументами в тексте ответа."
)


def looks_like_pseudo_tool_call(text) -> bool:
    """Модель описала вызов инструмента текстом вместо структурированного tool_calls."""
    if not text:
        return False
    low = text.lower()
    if "tool_call" in low or "<tool_call" in low or "<function_call" in low:
        return True
    return "```json" in low and any(
        k in low for k in ('"query"', '"arguments"', '"entitytype"', '"fqn"', '"filters"')
    )


CHARS_PER_TOKEN = 3  # грубая оценка для русского текста, если шлюз не вернул usage


def _estimate_tokens(obj) -> int:
    return max(1, len(json.dumps(obj, ensure_ascii=False, default=str)) // CHARS_PER_TOKEN)


class UsageTotals:
    """Накопитель токенов за один вопрос (сумма по всем вызовам модели)."""

    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0
        self.calls = 0
        self.seconds = 0.0
        self.estimated = False  # True, если хотя бы один вызов оценён, а не получен от шлюза

    def add(self, resp, messages, msg, elapsed: float = 0.0) -> dict:
        """Учесть один вызов модели; вернуть данные для события usage."""
        u = getattr(resp, "usage", None)
        p = getattr(u, "prompt_tokens", None) if u else None
        c = getattr(u, "completion_tokens", None) if u else None
        est = False
        if p is None or c is None:
            # Шлюз не вернул usage — оцениваем по длине текста.
            p = _estimate_tokens(messages)
            c = _estimate_tokens(msg.model_dump(exclude_none=True))
            est = True
            self.estimated = True
        self.prompt += p
        self.completion += c
        self.calls += 1
        self.seconds += elapsed
        return {"prompt": p, "completion": c, "total": p + c, "estimated": est,
                "seconds": round(elapsed, 1)}

    def as_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "completion": self.completion,
            "total": self.prompt + self.completion,
            "calls": self.calls,
            "seconds": round(self.seconds, 1),
            "estimated": self.estimated,
        }


LINEAGE_TOOL = "get_entity_lineage"
LINEAGE_MAX_NODES = 60


def _parse_json_loose(text: str):
    """JSON из текста результата инструмента; допускает обёртку-текст вокруг объекта."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        i, j = text.index("{"), text.rindex("}")
        return json.loads(text[i : j + 1])
    except (ValueError, json.JSONDecodeError):
        return None


def _node_key(x):
    if isinstance(x, dict):
        return x.get("id") or x.get("fullyQualifiedName") or x.get("fqn")
    return x


def _node_label(n: dict) -> str:
    fqn = n.get("fullyQualifiedName") or n.get("fqn") or ""
    return (
        n.get("displayName")
        or n.get("name")
        or (fqn.split(".")[-1] if fqn else None)
        or str(n.get("id", "?"))[:8]
    )


def extract_lineage(result_text: str):
    """
    Достаёт граф из результата get_entity_lineage. Формат REST OpenMetadata:
    {entity, nodes:[{id,name,type,...}], upstreamEdges:[{fromEntity,toEntity}],
    downstreamEdges:[...]}. Разбор терпимый к вложенности и к форме ссылок на узлы
    (id строкой или объектом). Возвращает {"nodes": {id: {label,type}}, "edges": {(a,b)}}
    или None, если связей не нашлось.
    """
    data = _parse_json_loose(result_text)
    if data is None:
        return None
    nodes: dict = {}
    edges: set = set()

    def add_node(n):
        if not isinstance(n, dict):
            return
        key = _node_key(n)
        if not key:
            return
        info = nodes.setdefault(key, {"label": None, "type": "entity"})
        info["label"] = info["label"] or _node_label(n)
        t = n.get("type") or n.get("entityType")
        if t:
            info["type"] = t

    def walk(d):
        if isinstance(d, list):
            for x in d:
                walk(x)
            return
        if not isinstance(d, dict):
            return
        is_lineage = any(k in d for k in ("nodes", "upstreamEdges", "downstreamEdges", "edges"))
        if is_lineage:
            add_node(d.get("entity"))
            for n in d.get("nodes") or []:
                add_node(n)
            for k in ("upstreamEdges", "downstreamEdges", "edges"):
                for e in d.get(k) or []:
                    if not isinstance(e, dict):
                        continue
                    a = _node_key(e.get("fromEntity") or e.get("from") or e.get("source"))
                    b = _node_key(e.get("toEntity") or e.get("to") or e.get("target"))
                    if a and b:
                        for end in (e.get("fromEntity"), e.get("toEntity")):
                            add_node(end)
                        edges.add((a, b))
        for v in d.values():
            if isinstance(v, (dict, list)):
                walk(v)

    def walk_flat(d):
        """Формат MCP OpenMetadata 2.0.2: {root, upstream:[{fromFQN,toFQN,fromName,toName,fromType,toType}], downstream:[...]}."""
        if isinstance(d, list):
            for x in d:
                walk_flat(x)
            return
        if not isinstance(d, dict):
            return
        for k in ("upstream", "downstream"):
            for e in d.get(k) or []:
                if not isinstance(e, dict):
                    continue
                a, b = e.get("fromFQN"), e.get("toFQN")
                if not a or not b:
                    continue
                for fqn, nm, tp in ((a, e.get("fromName"), e.get("fromType")), (b, e.get("toName"), e.get("toType"))):
                    info = nodes.setdefault(fqn, {"label": None, "type": "entity"})
                    info["label"] = info["label"] or nm or str(fqn).split(".")[-1]
                    if tp:
                        info["type"] = tp
                edges.add((a, b))
        for v in d.values():
            if isinstance(v, (dict, list)):
                walk_flat(v)

    walk(data)
    walk_flat(data)
    return {"nodes": nodes, "edges": edges} if edges else None


def merge_lineage(acc: dict, g: dict) -> None:
    for k, v in g["nodes"].items():
        cur = acc["nodes"].setdefault(k, {"label": None, "type": "entity"})
        cur["label"] = cur["label"] or v["label"]
        if v["type"] != "entity":
            cur["type"] = v["type"]
    acc["edges"] |= g["edges"]


def lineage_event(acc: dict):
    """Событие graph для страницы: компактные id n0..nK, подписи, типы, рёбра."""
    keys = []
    edges = []
    for a, b in sorted(acc["edges"], key=str):
        for k in (a, b):
            if k not in keys:
                keys.append(k)
        if len(keys) > LINEAGE_MAX_NODES:
            break
        edges.append((a, b))
    idx = {k: i for i, k in enumerate(keys[:LINEAGE_MAX_NODES])}
    nodes = [
        {"id": f"n{i}", "label": (acc["nodes"].get(k, {}).get("label") or str(k)[:8]),
         "type": acc["nodes"].get(k, {}).get("type", "entity")}
        for k, i in idx.items()
    ]
    return {
        "type": "graph",
        "nodes": nodes,
        "edges": [[f"n{idx[a]}", f"n{idx[b]}"] for a, b in edges if a in idx and b in idx],
        "truncated": len(keys) > LINEAGE_MAX_NODES,
    }


def mcp_tool_to_openai_schema(tool):
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.input_schema or {"type": "object", "properties": {}},
        },
    }


class McpBridge:
    """
    Держит одно живое MCP-соединение с OpenMetadata и один OpenAI-клиент на
    LLM-hub на весь жизненный цикл процесса — чтобы не переподключаться и не
    перечитывать список инструментов на каждый вопрос. Запросы к ask()
    сериализуются через лок: одно MCP-соединение одновременно обслуживает
    один диалог, что для демо вполне достаточно.
    """

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._session: Optional[ClientSession] = None
        self._tools: list = []
        self._openai_tools: list = []
        self._allowed_names: set = set()
        self._all_tools_count = 0
        self._lock = asyncio.Lock()
        self._client = OpenAI(
            base_url=LLM_HUB_BASE_URL,
            api_key=LLM_HUB_API_KEY,
            timeout=LLM_TIMEOUT,
            max_retries=0,  # повторы делает шлюз; на клиенте они лишь растягивают ожидание
        )

    async def connect(self) -> None:
        om_http_client = httpx.AsyncClient(headers={"Authorization": f"Bearer {OM_TOKEN}"})
        read, write = await self._stack.enter_async_context(
            streamablehttp_client(OM_BASE, http_client=om_http_client)
        )
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools_result = await session.list_tools()

        self._session = session
        self._all_tools_count = len(tools_result.tools)
        if TOOLS_ALLOW is None:
            self._tools = list(tools_result.tools)
        else:
            self._tools = [t for t in tools_result.tools if t.name in TOOLS_ALLOW]
            missing = TOOLS_ALLOW - {t.name for t in self._tools}
            if missing:
                print(f"[bridge] ВНИМАНИЕ: в MCP-сервере нет инструментов из белого списка: {sorted(missing)}")
        self._allowed_names = {t.name for t in self._tools}
        self._openai_tools = [mcp_tool_to_openai_schema(t) for t in self._tools]

    async def close(self) -> None:
        await self._stack.aclose()

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    @property
    def tools_info(self) -> list:
        return [{"name": t.name, "description": t.description or ""} for t in self._tools]

    @property
    def config_info(self) -> dict:
        return {
            "openmetadata_mcp_url": OM_BASE,
            "llm_hub_base_url": LLM_HUB_BASE_URL,
            "inference_model": INFERENCE_MODEL,
            "max_steps": MAX_STEPS,
            "tools_loaded": len(self._tools),
            "tools_total": self._all_tools_count,
        }

    async def ask(self, question: str) -> AsyncIterator[dict]:
        """Асинхронный генератор событий для одного вопроса: config -> (tool_call, tool_result)* -> final|limit_reached|error."""
        if self._session is None:
            yield {"type": "error", "message": "MCP-сессия не установлена (сервер ещё не подключился к OpenMetadata)"}
            return

        if self._lock.locked():
            # Мост обслуживает один вопрос за раз: сообщаем, что этот стоит в очереди.
            yield {"type": "queued"}
        async with self._lock:
            yield {"type": "config", **self.config_info}

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ]

            pseudo_retries_left = PSEUDO_TOOL_RETRIES
            usage = UsageTotals()
            lineage_acc = {"nodes": {}, "edges": set()}

            try:
                for step in range(MAX_STEPS):
                    kwargs = {}
                    if TEMPERATURE is not None:
                        kwargs["temperature"] = TEMPERATURE
                    t0 = time.monotonic()
                    resp = await asyncio.to_thread(
                        self._client.chat.completions.create,
                        model=INFERENCE_MODEL,
                        messages=messages,
                        tools=self._openai_tools,
                        **kwargs,
                    )
                    msg = resp.choices[0].message

                    step_usage = usage.add(resp, messages, msg, time.monotonic() - t0)
                    yield {"type": "usage", "step": step, **step_usage}

                    if not msg.tool_calls:
                        if pseudo_retries_left > 0 and looks_like_pseudo_tool_call(msg.content):
                            # Модель написала вызов текстом. Не показываем это как ответ:
                            # просим повторить через настоящий вызов инструмента.
                            pseudo_retries_left -= 1
                            yield {"type": "notice", "message": "модель описала вызов текстом — повторяю запрос"}
                            messages.append({"role": "assistant", "content": msg.content or ""})
                            messages.append({
                                "role": "user",
                                "content": (
                                    "Ты написал вызов инструмента текстом. Выполни его через "
                                    "механизм function calling (tool_calls), а не текстом."
                                ),
                            })
                            continue
                        if lineage_acc["edges"]:
                            yield lineage_event(lineage_acc)
                        yield {"type": "final", "content": msg.content, "steps": step, "usage": usage.as_dict()}
                        return

                    messages.append(msg.model_dump(exclude_none=True))

                    for tool_call in msg.tool_calls:
                        name = tool_call.function.name
                        try:
                            args = json.loads(tool_call.function.arguments or "{}")
                        except json.JSONDecodeError:
                            args = {}

                        yield {"type": "tool_call", "name": name, "args": args, "step": step}

                        if name not in self._allowed_names:
                            # Защита: модель попросила инструмент вне белого списка (например, запись).
                            result_text = f"Инструмент {name} недоступен. Используй только: {', '.join(sorted(self._allowed_names))}."
                        else:
                            result = await self._session.call_tool(name, args)
                            result_text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
                        preview = result_text[:TOOL_RESULT_PREVIEW_CHARS]

                        if name == LINEAGE_TOOL:
                            g = extract_lineage(result_text)
                            if g:
                                merge_lineage(lineage_acc, g)
                                print(f"[bridge] lineage: разобрано узлов {len(g['nodes'])}, связей {len(g['edges'])}", flush=True)
                            else:
                                # Формат ответа мог отличаться от ожидаемого — пишем в журнал для отладки.
                                print(f"[bridge] lineage: связей не найдено/не разобрано: {result_text[:1500]!r}", flush=True)

                        yield {
                            "type": "tool_result",
                            "name": name,
                            "preview": preview,
                            "length": len(result_text),
                            "step": step,
                        }

                        messages.append(
                            {"role": "tool", "tool_call_id": tool_call.id, "content": result_text}
                        )

                yield {"type": "limit_reached", "steps": MAX_STEPS, "usage": usage.as_dict()}
            except openai.APITimeoutError:
                yield {"type": "error",
                       "message": f"Модель не ответила за {int(LLM_TIMEOUT)} с. Повторите вопрос или смените модель (INFERENCE_MODEL).",
                       "usage": usage.as_dict()}
            except Exception as exc:  # демо: не роняем сервер на ошибке одного вопроса
                yield {"type": "error", "message": f"{type(exc).__name__}: {exc}", "usage": usage.as_dict()}
