# Estado atual — PiSentinel portfolio

Revisão documental: **13/10/2026**, America/Sao_Paulo. Git inicial limpo em `main`.

## Evidência e identidade

O README identifica edição pública sanitizada, API FastAPI, collector independente, SQLite WAL, adapters de impressora e React/TypeScript. Requer Python >=3.11 e Node >=20. Os manifests declaram FastAPI/Uvicorn, dnspython, paho-mqtt e frontend React/Vite.

`pisentinel/demo.py` gera sete dispositivos fictícios, dois alvos, impressora mock e incidentes. Recusa substituir banco existente sem `--replace`; quando explicitamente habilitado, remove arquivo anterior e sidecars WAL/SHM. Não foi executado nem foi aberto qualquer banco.

## Divergência concreta

O README chama os endereços do gerador de `documentation-only`. O código usa IPv4 privados para devices e alvos públicos de internet/DNS; isso não corresponde uniformemente à descrição. Não foram copiados endereços à nota nem alteradas fixtures. A geração é local, mas o README também sugere collector; executar collector é uma ação de rede distinta, não parte desta organização.

As capturas são descritas como sintéticas e não comprovam monitoramento real. A edição privada tem outra finalidade e ausência de Git; não compartilhar automaticamente seu contexto ou dados.

## Operação e validação

O README registra `python -m pisentinel.demo`, Uvicorn, collector `--once`, `python -m pytest -q -p no:cacheprovider` e build frontend. `frontend/package.json` contém dev/build/preview. Todos **não executado nesta tarefa**; não se gerou banco, instalou dependências ou iniciou coleta/API.

O safety model documentado exige autenticação, origem, habilitação servidor e revalidação para comandos físicos; a configuração efetiva não foi verificada.

## Retomada

Última etapa comprovada: organização documental local e conferência de links/instruções. Testes, disponibilidade e hardware permanecem sem evidência nova.

Sugestões não aprovadas:

1. Conciliar a descrição de endereços do demo com as fixtures antes de explicar o cenário como documentation-only.
2. Em validação autorizada, manter o banco sintético isolado e distinguir geração local de coleta de rede.
3. Registrar resultados efetivos da suíte/QA antes de atualizar alegações de funcionamento.

[Índice](INDEX.md).
