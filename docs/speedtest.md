# Teste de velocidade Ookla

O PiSentinel executa o cliente oficial `speedtest` em um processo separado do coletor de disponibilidade. O teste mede o caminho do Raspberry até um servidor Ookla; ele não mede a velocidade de cada dispositivo e não substitui os testes locais de ICMP, TCP e DNS.

## Instalação no Raspberry Pi OS 64-bit

Use somente o pacote oficial da Ookla para `arm64`. O instalador do projeto reconhece Raspberry Pi OS/Debian Bookworm e Trixie, baixa a versão oficial `1.2.0.84`, verifica o SHA-256 publicado e só então instala. O serviço espera o binário em `/usr/bin/speedtest`. A execução inclui `--accept-license` e `--accept-gdpr`; portanto, habilitar o serviço registra a aceitação dos termos do cliente Ookla.

```bash
cd /opt/pi-sentinel
sudo sh scripts/install_ookla_speedtest.sh
```

## Funcionamento

- intervalo padrão: 6 horas;
- primeiro teste automático: somente depois de um intervalo completo;
- execução manual: botão **Testar agora**, que apenas enfileira o trabalho;
- exclusão mútua: um único worker e uma única fila impedem testes simultâneos;
- timeout padrão: 180 segundos;
- uma repetição controlada em caso de falha;
- retenção padrão: 90 dias;
- incidentes: duas medições consecutivas com falha ou abaixo de um limite configurado; uma medição saudável recupera o incidente;
- limites de download e upload começam vazios, pois dependem do plano contratado.

São persistidos download, upload, ping, jitter, perda de pacotes quando informada, duração, servidor, ISP e bytes transferidos. IP externo, IP interno, MAC, URL pública do resultado e qualquer campo não necessário são descartados pelo normalizador.

## API

- `GET /api/speedtests?days=1|7|30`
- `GET /api/speedtests/config`
- `PATCH /api/speedtests/config`
- `POST /api/speedtests/run`

O consumo mensal exibido é a soma de `download.bytes + upload.bytes` devolvida pelo cliente. É uma estimativa do tráfego do teste, não uma medição de franquia do provedor.

Durante a medição, o cliente tenta usar a capacidade disponível de download e upload. Isso pode elevar a latência e causar buffering temporário em vídeos, jogos ou chamadas. O histórico de sondagens do PiSentinel pode ser usado para confirmar essa correlação pelo horário; o agendamento deve considerar os períodos de menor uso da rede.
