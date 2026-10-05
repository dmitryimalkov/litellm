workspace "MCP-мост OpenMetadata" "C4-модель моста между LLM-hub и каталогом OpenMetadata демо-стенда. Точки сопряжения, которые меняются при переносе в dev-среду, отмечены тегом IntegrationPoint, свойством integration-point и комментариями прямо в DSL." {

    !identifiers hierarchical

    model {

        operator = person "Оператор" "Задаёт вопросы боту в терминале во время демонстрации" "Person"

        mcpBridge = softwareSystem "MCP-мост" "Ведёт цикл function calling между LLM-hub и каталогом OpenMetadata: передаёт вопрос и схемы MCP-инструментов модели, выполняет запрошенные ею вызовы инструментов" "InternalSystem" {
            bridgeScript = container "mcp_bridge.py" "MCP-клиент + OpenAI-клиент. Системный промпт, лимит шагов (MCP_BRIDGE_MAX_STEPS), преобразование схем MCP-инструментов в формат OpenAI tools" "Python 3, mcp SDK, openai SDK"
        }

        // =====================================================================
        // ТОЧКА СОПРЯЖЕНИЯ 1 — LLM-HUB
        // Сейчас: self-hosted LiteLLM на той же ВМ, что и mcp_bridge.py
        // (http://127.0.0.1:4000/v1, мастер-ключ в .env).
        // В dev-среде: другой LiteLLM-инстанс (другой хост/ключ) или
        // вообще другой OpenAI-совместимый шлюз. Меняются только
        // переменные LLM_HUB_BASE_URL / LLM_HUB_API_KEY — mcp_bridge.py
        // не правится.
        // =====================================================================
        llmHub = softwareSystem "LLM-hub (LiteLLM)" "OpenAI-совместимый шлюз: принимает chat/completions с tools, маршрутизирует на провайдера инференса, выдаёт виртуальные ключи" "External,IntegrationPoint" {
            properties {
                "integration-point" "LLM-hub — меняется при переносе в dev"
                "env-vars" "LLM_HUB_BASE_URL, LLM_HUB_API_KEY"
                "prod-value" "http://127.0.0.1:4000/v1 (cloud.ru, self-hosted)"
            }
        }

        // =====================================================================
        // ТОЧКА СОПРЯЖЕНИЯ 2 — INFERENCE MODEL / PROVIDER
        // Сейчас: cloud.ru Foundation Models, модель "deepseek-v3.2"
        // (алиас заведён в LiteLLM).
        // В dev-среде: своя ML-модель под своим алиасом в LiteLLM/шлюзе.
        // Обязательное условие — модель должна поддерживать OpenAI tool
        // calling, иначе мост не сможет вызывать MCP-инструменты.
        // =====================================================================
        inferenceProvider = softwareSystem "Провайдер инференса (cloud.ru Foundation Models)" "Выполняет инференс модели deepseek-v3.2 по запросу LLM-hub" "External,IntegrationPoint" {
            properties {
                "integration-point" "Inference model — меняется при переносе в dev"
                "env-vars" "INFERENCE_MODEL (алиас модели в LLM-hub)"
                "prod-value" "deepseek-v3.2"
                "requirement" "модель обязана поддерживать OpenAI tool calling (function calling)"
            }
        }

        openMetadata = softwareSystem "OpenMetadata" "Каталог данных демо-стенда: схемы, PII-теги, RLS-политики, lineage; отдаёт их через MCP-сервер" "External" {
            mcpServer = container "MCP Server" "/mcp эндпоинт, Streamable HTTP. Инструменты: search_metadata, get_entity_details, get_entity_lineage, create_lineage, root_cause_analysis и др." "OpenMetadata 2.0.2"
            catalogDb = container "Catalog DB + Search" "Метаданные (Postgres), поисковый индекс (Elasticsearch)" "Postgres, Elasticsearch"

            mcpServer -> catalogDb "Читает/пишет метаданные"
        }

        demoStand = softwareSystem "Demo stand" "Multi-tenant демо-стенд: источники данных, которые каталогизирует OpenMetadata. Не часть live-пути вопрос/ответ бота" "External" {
            ad = container "AD/LDAP" "Идентичность, тенантные группы (CompanyA-*, CompanyB-*)"
            keycloak = container "Keycloak" "SSO"
            opa = container "OPA" "Политики авторизации (authz.rego), тенантная изоляция"
            postgres = container "Postgres" "salesdb, sales_transactions, RLS-политики"
            clickhouse = container "ClickHouse" "audit db"
            airflow = container "Airflow" "DAG-и загрузки данных company_a / company_b"
            minio = container "MinIO" "S3-совместимое хранилище датасетов (company_a/raw, company_b/raw)"
            superset = container "Superset" "BI-дашборды поверх Postgres"
        }

        // ---- живой путь вопрос/ответ во время демо ----
        operator -> mcpBridge.bridgeScript "Задаёт вопрос" "SSH, терминал"
        mcpBridge.bridgeScript -> llmHub "Отправляет вопрос + схемы MCP-инструментов, получает ответ или запрос на вызов инструмента" "HTTP, OpenAI Chat Completions API"
        llmHub -> inferenceProvider "Выполняет инференс" "HTTPS, Foundation Models API"
        mcpBridge.bridgeScript -> openMetadata.mcpServer "Вызывает MCP-инструмент каталога" "Streamable HTTP, Bearer JWT (бот mcpapplicationbot)"

        // ---- отдельно настроенный путь наполнения каталога (не live-демо) ----
        openMetadata -> demoStand.postgres "Коннектор demo-stand-postgres: читает схему/сэмплы" "JDBC"
        openMetadata -> demoStand.clickhouse "Коннектор ClickHouseMeta-demo" "HTTP"
        openMetadata -> demoStand.airflow "Коннектор demo-stand-airflow: список DAG-ов" "REST API, LDAP service account openmetadata-svc"
        openMetadata -> demoStand.minio "Коннектор demo-stand-minio: манифест openmetadata.json" "S3 API"
        openMetadata -> demoStand.superset "Коннектор demo-stand-superset: дашборды" "Superset REST API"
    }

    views {

        systemContext mcpBridge "Context" {
            include *
            description "Контекст MCP-моста: оператор, две точки сопряжения с LLM-инфраструктурой, каталог OpenMetadata и демо-стенд как источник данных каталога"
            autolayout lr
        }

        container mcpBridge "BridgeContainers" {
            include *
            description "Единственный контейнер моста и его внешние зависимости"
            autolayout lr
        }

        container openMetadata "CatalogContainers" {
            include *
            description "Внутреннее устройство OpenMetadata: MCP-сервер и хранилище метаданных"
            autolayout lr
        }

        container demoStand "DemoStandContainers" {
            include *
            description "Сервисы демо-стенда, подключённые к OpenMetadata как источники"
            autolayout lr
        }

        dynamic mcpBridge "DemoFlow" "Последовательность одного вопроса во время демонстрации" {
            operator -> mcpBridge.bridgeScript "1. Задаёт вопрос"
            mcpBridge.bridgeScript -> llmHub "2. Вопрос + схемы инструментов"
            llmHub -> inferenceProvider "3. Инференс"
            llmHub -> mcpBridge.bridgeScript "4. Решение: вызвать инструмент"
            mcpBridge.bridgeScript -> openMetadata.mcpServer "5. Вызов MCP-инструмента"
            openMetadata.mcpServer -> mcpBridge.bridgeScript "6. Результат"
            mcpBridge.bridgeScript -> llmHub "7. Результат инструмента в диалог"
            llmHub -> mcpBridge.bridgeScript "8. Финальный ответ"
            mcpBridge.bridgeScript -> operator "9. Печатает ответ в терминал"
            autolayout lr
        }

        styles {
            element "Person" {
                shape person
                background #08427b
                color #ffffff
            }
            element "InternalSystem" {
                background #1168bd
                color #ffffff
            }
            element "External" {
                background #8a8d91
                color #ffffff
            }
            element "IntegrationPoint" {
                background #b5563a
                color #ffffff
                stroke #7a3824
                strokeWidth 3
            }
            element "Container" {
                background #ffffff
                color #1b1f23
                stroke #5b6472
            }
        }
    }

}
