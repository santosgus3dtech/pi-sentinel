# Discovery seguro da Creality K1C

Esta ferramenta reúne evidências sobre os serviços expostos por **uma única impressora**. Ela não é um adapter de produção e não altera a K1C: não autentica, não envia G-code, não executa comandos, não inicia ou cancela impressões e não modifica arquivos ou configurações.

## Execução

No diretório do PiSentinel:

```bash
python scripts/discover_k1c.py --host 192.168.10.155
```

Para obter o resultado normalizado:

```bash
python scripts/discover_k1c.py --host 192.168.10.155 --json
```

O host deve ser um IPv4 privado. O timeout padrão é de 2 segundos por operação e pode ser ajustado entre 0,1 e 10 segundos com `--timeout`.

## O que é testado

- **Host:** uma tentativa ICMP e o resultado agregado das portas TCP limitadas.
- **Portas:** somente 80, 443, 8080, 9999 e 7125 no host informado. A porta 9999 foi comprovada como WebSocket de telemetria no firmware analisado. A porta 7125 continua sendo apenas uma candidata; estar aberta não confirma Moonraker.
- **HTTP/HTTPS:** `GET /` com timeout e corpo limitado a 1 MiB. Certificados locais autoassinados podem ser lidos para discovery.
- **Interface web:** até 12 arquivos JavaScript da mesma impressora. O código é inspecionado em busca de URLs estáticas usadas por `fetch`, Axios, XHR, WebSocket e câmera.
- **API:** no máximo 12 endpoints de leitura encontrados na própria interface. Caminhos com termos de controle, impressão, upload, atualização, movimento ou G-code são recusados.
- **Moonraker:** só é confirmado quando `/server/info` retorna JSON com uma assinatura estrutural compatível. Uma porta aberta, um nome de arquivo ou um texto solto não bastam.
- **WebSocket:** somente URLs estáticas encontradas na interface recebem um handshake HTTP de abertura. Nenhuma mensagem é enviada depois do handshake.
- **Câmera:** somente URLs encontradas na interface são consultadas. O protocolo é inferido de evidência como `Content-Type` MJPEG, HLS, imagem ou vídeo. Uma referência RTSP é registrada, mas o stream RTSP não é aberto.

Todas as URLs permanecem restritas ao IP informado. Recursos externos encontrados no HTML ou JavaScript são ignorados. Tokens, senhas, chaves, sessões e assinaturas em query strings são mascarados na saída; corpos das respostas nunca são incluídos no relatório.

## Como interpretar

`available` significa que o teste recebeu evidência positiva naquele momento. `detected` significa que a interface menciona o recurso, mas a disponibilidade ou o protocolo ainda não foi confirmado.

**Não encontrado** significa apenas que essas verificações limitadas não produziram evidência. Pode haver um serviço em outra porta, uma URL construída dinamicamente, autenticação, bloqueio de ICMP ou diferença entre versões de firmware.

**Não suportado** significa que o discovery deliberadamente não executa aquele teste. Por exemplo, ele não abre streams RTSP, não executa protocolos proprietários e não tenta autenticação. A ausência de evidência nunca é apresentada como prova de que a K1C não suporta o recurso.

## Limitações

- Não é um scanner completo de portas nem de rede.
- JavaScript compactado pode construir URLs em tempo de execução que a análise estática não recupera.
- Uma API protegida pode aparecer como detectada sem que seu conteúdo seja acessível.
- O resultado descreve o momento da execução e pode mudar após atualização de firmware ou configuração feita pelo proprietário.
- O relatório serve para orientar a futura implementação do `CrealityK1CAdapter`; ele não implementa esse adapter.
