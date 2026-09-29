# Arquitetura de impressoras

O módulo de impressoras é separado do coletor de rede. Ele monitora, apresenta os parâmetros confirmados por cada protocolo e pode iniciar um arquivo existente quando o administrador habilita explicitamente os controles.

## Fluxo

1. `Database.list_printers()` lê as definições e faz `LEFT JOIN` com `devices`.
2. `PrinterService` transforma cada registro em `PrinterDefinition` e escolhe o adapter por `adapter_type`.
3. O adapter devolve estado, temperaturas, trabalho atual, câmera, saúde e capacidades em fragmentos normalizados. Recursos opcionais incluem parâmetros, arquivos, inspeção, miniatura, histórico e início de impressão.
4. O serviço compõe `NormalizedPrinter`. Qualquer exceção fica restrita à impressora que falhou e vira um snapshot `ERROR/unavailable`.
5. A API usa `GET` para consulta. O único comando físico disponível é `POST /api/printers/{id}/print`, protegido por uma flag do servidor, origem do painel e confirmação literal.

## Banco

`printers` contém configuração e identidade da integração: `device_id`, nome, fabricante, modelo, tipo/configuração do adapter, flags e datas. `device_id` é opcional e único quando preenchido. IP, MAC e estado de rede são derivados de `devices` e não são duplicados.

A criação usa `CREATE TABLE IF NOT EXISTS` no mesmo fluxo idempotente já usado por `Database`. Bancos anteriores recebem a tabela ao abrir, sem reescrever as tabelas existentes.

## Mock

`MockPrinterAdapter` suporta `IDLE`, `PRINTING`, `PAUSED`, `ERROR` e `OFFLINE`. Em `PRINTING` e `PAUSED`, ele devolve dados determinísticos de progresso, temperaturas, arquivo e tempo restante. O mock de interface é opt-in por variável de ambiente e não persiste dados.

## Creality K1C

`CrealityK1CAdapter` usa a telemetria JSON comprovada no WebSocket stock da porta 9999. A interface HTTP serve como fallback parcial e a câmera é isolada do estado principal. Consulte `docs/k1c-integration.md` para protocolo, estados, timeouts e limitações.

## Bambu Lab A1

`BambuA1Adapter` usa MQTT 3.1.1 sobre TLS na porta 8883. A conexão é persistente, recebe atualizações por evento e solicita um snapshot com `pushall`. A leitura de projetos no cartão usa FTPS implícito na porta 990, também com pin de certificado. Credenciais e pins vêm exclusivamente do ambiente. Consulte `docs/bambu-a1-integration.md`.

## Controles

`PISENTINEL_PRINTER_CONTROLS_ENABLED=false` é o padrão. Quando habilitada, a ação de impressão ainda exige:

1. requisição JSON de mesma origem enviada pelo painel;
2. confirmação `IMPRIMIR`;
3. arquivo retornado pela própria impressora e marcado como imprimível;
4. impressora fora de `PRINTING` e `PAUSED`;
5. confirmação do protocolo quando ele fornece resposta.

Falhas permanecem isoladas no adapter. Não há upload, exclusão, renomeação, pausa, cancelamento, aquecimento, movimento ou envio arbitrário de G-code.
