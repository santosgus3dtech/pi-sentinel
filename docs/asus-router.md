# Integração somente leitura com ASUSWRT

O PiSentinel consulta a interface local do roteador ASUS RT-AX82U para complementar ARP, ICMP e DNS. A integração não chama `apply.cgi`, não altera Wi-Fi/QoS, não reinicia o roteador e não guarda cookie de sessão no banco.

## Configuração

As credenciais ficam apenas no arquivo privado `/etc/pi-sentinel/pisentinel.env`, que deve pertencer a `root:pisentinel` e ter modo `640`, permitindo leitura somente ao serviço:

```dotenv
PISENTINEL_ROUTER_HOST=192.168.10.1
PISENTINEL_ROUTER_USERNAME=
PISENTINEL_ROUTER_PASSWORD=
PISENTINEL_ROUTER_POLL_SECONDS=30
PISENTINEL_ROUTER_LOG_POLL_SECONDS=300
```

Não coloque valores reais em Git. A API e o frontend nunca retornam usuário, senha, cookie, PIN WPS, número de série ou identificador interno de sessão.

## Dados coletados

- modelo e versão do firmware;
- disponibilidade e latência da consulta ao roteador;
- CPU e memória;
- quantidade de clientes online e conhecidos;
- interface por dispositivo: Ethernet, Wi-Fi 2,4 GHz ou Wi-Fi 5 GHz;
- RSSI, padrão PHY, largura de canal, fluxos espaciais e taxas Tx/Rx negociadas;
- tempo conectado, método de IP e tempo restante da concessão DHCP;
- canal, largura, ruído, utilização e indicação de interferência de cada rádio;
- contadores agregados WAN, Ethernet e Wi-Fi, convertidos em Mbps entre duas amostras.
- eventos normalizados de associação, reconexão e desconexão Wi-Fi, consultados a cada cinco minutos.

As taxas Tx/Rx dos clientes são taxas PHY negociadas, não o consumo instantâneo de cada dispositivo. O PiSentinel não captura pacotes nem conteúdo de navegação. O histórico detalhado por aplicativo/cliente do ASUS Traffic Analyzer permanece desativado enquanto esse recurso estiver desligado no roteador.

O log bruto do ASUSWRT é consultado por um recurso somente leitura da própria página de log e não é salvo. Somente horário, MAC, tipo de evento, motivo curto e RSSI são normalizados. Eventos são correlacionados ao cadastro existente pelo MAC, retidos pelo mesmo período das amostras de rede e usados para destacar aparelhos com três ou mais eventos de desconexão em 24 horas. Uma queda física pode produzir mais de um evento no firmware, portanto o total não representa necessariamente quedas independentes.

## Banco e API

- `router_device_observations`: última observação do roteador ligada ao `devices.id` pelo MAC;
- `router_samples`: amostras sanitizadas para retenção e evolução futura;
- `router_wifi_events`: eventos Wi-Fi normalizados, sem o conteúdo bruto do log;
- `metadata.router_status`: estado atual usado pela API;
- `GET /api/router`: painel atual e clientes correlacionados, restrito ao administrador;
- `GET /api/devices/{id}`: inclui o objeto `router` quando houver correlação.

Uma falha de autenticação ou timeout atualiza somente o estado do roteador como indisponível. O collector continua executando descoberta, probes, impressoras e incidentes.

## Limitações

Os endpoints são internos do ASUSWRT e podem mudar após uma atualização de firmware. Os parsers são conservadores e falham de forma isolada. O primeiro cálculo de tráfego precisa de duas amostras. Clientes com MAC privado podem mudar de identidade quando o aparelho troca o endereço aleatório.
