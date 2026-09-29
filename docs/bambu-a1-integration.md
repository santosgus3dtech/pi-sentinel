# Integração da Bambu Lab A1

## Evidência utilizada

A A1 real foi observada na rede local antes da implementação. O anúncio SSDP identificou o modelo `N2S`, firmware `01.08.01.00` e link seguro. As portas locais 8883, 990 e 6000 responderam; HTTP, HTTPS e RTSP 322 não responderam.

A porta 8883 confirmou MQTT 3.1.1 sobre TLS. O certificado apresentado tem como `CN` o serial da impressora e é assinado pela CA privada da Bambu. A autenticação local usa o usuário fixo `bblp` e o Access Code da impressora. O PiSentinel assina somente `device/<serial>/report`.

Ao conectar, o adapter publica a consulta `pushing/pushall` em `device/<serial>/request`. Ela solicita o snapshot inicial. Depois disso, a conexão permanece aberta e incorpora as atualizações incrementais enviadas pelo broker.

O snapshot real confirmou os campos `gcode_state`, `mc_percent`, `mc_remaining_time`, `subtask_name`, `gcode_file`, `nozzle_temper`, `nozzle_target_temper`, `bed_temper`, `bed_target_temper` e `print_error`.

## Configuração

Defina as variáveis somente em `.env` local ou, no Raspberry, em `/etc/pi-sentinel/pisentinel.env`:

```dotenv
BAMBU_A1_HOST=
BAMBU_A1_SERIAL=
BAMBU_A1_ACCESS_CODE=
BAMBU_A1_CERT_SHA256=
```

O `.env` está ignorado pelo projeto e os arquivos `.env.example` não contêm valores reais.

- Consulte o serial em **Configurações / Informações do dispositivo** na tela da A1 ou no cadastro local do Bambu Studio.
- Consulte o Access Code na seção de rede LAN da impressora. Os nomes podem variar entre versões; não copie esse código para documentação, banco ou configuração do frontend.
- Obtenha o SHA-256 do certificado diretamente na rede confiável e confira se o `CN` corresponde ao serial:

```bash
openssl s_client -connect "$BAMBU_A1_HOST:8883" -showcerts </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -fingerprint -sha256
```

Remova os dois-pontos do fingerprint ou mantenha-os; o adapter normaliza o valor. O pin é verificado no callback de abertura do socket TLS, antes de o MQTT transmitir as credenciais.

Cadastre a impressora sem duplicar o device:

```bash
cd /opt/pi-sentinel
./.venv/bin/python scripts/register_bambu_a1.py --database /var/lib/pi-sentinel/pisentinel.db
```

O script procura primeiro o MAC informado. IP, MAC e disponibilidade permanecem em `devices`.

## Estados e dados

| Estado MQTT | PiSentinel |
| --- | --- |
| `IDLE` | `IDLE` |
| `RUNNING`, `PRINTING` | `PRINTING` |
| `PAUSE`, `PAUSED` | `PAUSED` |
| `FINISH`, `FINISHED`, `COMPLETED` | `COMPLETED` |
| `FAILED`, `ERROR` | `ERROR` |
| outro | `UNKNOWN` |

`mc_remaining_time` é informado pela A1 em minutos e convertido para segundos no modelo normalizado. `subtask_name` é preferido como nome do trabalho, com fallback para `gcode_file`. Códigos não nulos em `print_error`, `fail_reason`, `mc_print_error_code` ou alertas HMS aparecem como erro da impressora sem derrubar a conexão.

## Saúde e resiliência

Os componentes são independentes:

- **Rede:** conexão TCP com o broker local.
- **Telemetria:** autenticação, assinatura e resposta MQTT.
- **Câmera:** desativada até haver um provider confirmado.

Falha de autenticação ou protocolo mantém a A1 como `PARTIAL` quando a porta de rede responde. Falha total da rede produz `OFFLINE`. Cada adapter é isolado pelo `PrinterService`, então a K1C e o monitoramento de rede continuam operando.

A conexão MQTT permanece aberta e processa atualizações por evento. O `PrinterService` reutiliza o snapshot por 8 segundos durante impressão/pausa e 20 segundos nos demais estados; o adapter não abre uma nova sessão em cada atualização do frontend.

## Câmera

A porta 6000 da A1 respondeu com TLS e apresentou o mesmo certificado da impressora. O protocolo de vídeo por trás dessa porta não foi confirmado em uma fonte oficial nem validado como stream diretamente utilizável pelo navegador.

Por isso `camera=false`, não há relay de vídeo e o Access Code nunca é entregue ao browser. A câmera não bloqueia temperaturas, trabalho, progresso ou saúde MQTT.

## Cartão e impressão

A A1 real confirmou FTPS implícito na porta 990. O adapter:

- valida o mesmo pin SHA-256 antes do login;
- usa `LIST` somente na raiz do cartão;
- aceita como imprimíveis somente arquivos não vazios `.gcode.3mf` ou `.3mf`;
- baixa o projeto apenas para memória/arquivo temporário do backend, com limite de 700 MiB;
- abre o ZIP 3MF com limites de quantidade e tamanho, sem extrair caminhos no sistema;
- lê placas `Metadata/plate_<n>.gcode`, metadados de fatiamento e miniaturas.

Para iniciar, o backend verifica novamente o arquivo e a placa, rejeita a operação se a impressora estiver imprimindo/pausada e envia `print/project_file` pelo MQTT já autenticado. O comando leva a URL FTPS local do arquivo, a placa interna e somente as opções apresentadas no diálogo. Quando AMS Lite é usado, todos os filamentos precisam ser mapeados. O MQTT precisa responder com a mesma sequência e comando; uma recusa ou timeout vira erro isolado.

Esse comando foi coberto por testes com respostas capturadas/simuladas, mas não foi disparado na A1 real durante o deploy porque ela estava ocupada. O primeiro teste físico deve ser feito com a impressora ociosa e um projeto pequeno já conhecido.

## Limites

O PiSentinel não envia arquivos, exclui, renomeia, pausa, cancela, aquece, move eixos ou altera configurações. A integração não depende da nuvem Bambu. O histórico local da A1 e o protocolo de câmera da porta 6000 continuam indisponíveis porque não foram confirmados de forma segura.
