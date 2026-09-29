# Integração da Creality K1C

## Evidência utilizada

A interface stock da K1C foi consultada a partir do Raspberry na mesma rede. O equipamento respondeu em HTTP e HTTPS com `Server: libhv/1.3.3`. A interface oficial carregou `app.b05d1c1a.js`, que abre `ws://<ip>:9999`.

O handshake real da porta 9999 retornou HTTP 101 e o servidor enviou, sem pedido de status, um snapshot JSON com os campos usados pelo adapter: `state`, `printProgress`, `printFileName`, `printLeftTime`, `nozzleTemp`, `targetNozzleTemp`, `bedTemp0` e `targetBedTemp0`. Depois do snapshot, o mesmo socket enviou atualizações incrementais de temperatura.

Não houve resposta na porta 7125 e nenhuma assinatura Moonraker foi encontrada. O adapter não usa Moonraker, scraping HTML, SSH, root ou comandos Klipper.

## Estados normalizados

A própria interface oficial apresenta a tabela de estados abaixo:

| Código K1C | Texto da interface | PiSentinel |
| --- | --- | --- |
| 0 | Printing stopped | `IDLE` |
| 1 | Printing | `PRINTING` |
| 2 | Printing complete | `COMPLETED` |
| 3 | Printing failed | `ERROR` |
| 4 | Print abort | `ERROR` |
| 5 | Printing paused | `PAUSED` |

Qualquer outro valor vira `UNKNOWN`.

## Saúde e fallback

O WebSocket é a fonte primária dos dados. O snapshot abre uma conexão, recebe os dados iniciais e fecha. Arquivos e histórico usam consultas pontuais copiadas da interface oficial. Uma falha recebe no máximo uma repetição controlada.

Se a telemetria falhar, o adapter testa `HEAD /` na interface HTTP. Quando o site responde, a impressora permanece online com saúde agregada `PARTIAL`, preservando `connection_status: degraded` para compatibilidade, e os dados de impressão ficam desconhecidos. Quando web e telemetria falham, o estado agregado fica `OFFLINE`.

Rede, web, API e câmera aparecem como componentes independentes. Falha de câmera produz saúde parcial e não torna a impressora offline.

O `PrinterService` mantém snapshots por 8 segundos durante impressão/pausa e por 20 segundos nos demais estados. Offline usa 15 segundos. A interface consulta a cada 8 segundos durante impressão/pausa e 20 segundos nos demais estados.

## Câmera

O JavaScript oficial coloca em uma tag `<img>` a URL `http://<ip>:8080/?action=stream`. O Raspberry confirmou que a porta 8080 estava recusando conexão durante a implementação, embora a telemetria anunciasse `video: 1`.

O provider consulta esse endpoint e só aceita `multipart/x-mixed-replace` ou uma imagem HTTP conhecida. Quando `multipart/x-mixed-replace` responder, o protocolo é identificado como MJPEG. O navegador usa `/api/printers/{id}/camera`; o backend conecta ao IP associado ao device e transmite o conteúdo permitido. Nenhuma URL, senha ou token da impressora é recebido do navegador.

Enquanto a porta 8080 não responder, a aba mostra “Câmera indisponível” e o restante do monitoramento continua funcionando.

## Cadastro idempotente

No Raspberry:

```bash
cd /opt/pi-sentinel
./.venv/bin/python scripts/register_k1c.py --database /var/lib/pi-sentinel/pisentinel.db
```

O script procura primeiro o MAC `fc:ee:28:00:00:01`. Se o device já existe, usa seu `id`; se uma impressora já está associada a ele, atualiza o mesmo registro. IP e MAC continuam apenas em `devices`.

## API

- `GET /api/printers/{id}/health`: saúde geral e por componente.
- `GET /api/printers/{id}/camera`: proxy do stream quando disponível.
- `GET /api/printers/{id}/details`: snapshot ampliado com camadas, tempos, velocidades, fluxo, ventoinhas, sensores e parâmetros confirmados.
- `GET /api/printers/{id}/files`: arquivos retornados por `reqGcodeFile`.
- `GET /api/printers/{id}/files/inspect`: metadados e disponibilidade de miniatura.
- `GET /api/printers/{id}/files/preview`: proxy limitado da miniatura.
- `GET /api/printers/{id}/history`: trabalhos retornados por `reqHistory`.
- `POST /api/printers/{id}/print`: inicia um caminho já listado usando `opGcodeFile=printprt:<path>`.

O comando de impressão foi extraído do JavaScript da própria interface stock. Ele só é enviado quando os controles do servidor estão habilitados, a impressora está ociosa, o caminho pertence a `/usr/data/printer_data/gcodes/` e o usuário confirma `IMPRIMIR`. A aplicação não permite fornecer G-code arbitrário, enviar arquivo, excluir, renomear, pausar, cancelar, aquecer ou movimentar.
