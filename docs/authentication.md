# Autenticação do PiSentinel

O login é opcional no código e deve ser habilitado em produção com:

```ini
PISENTINEL_AUTH_ENABLED=true
PISENTINEL_AUTH_SESSION_HOURS=12
PISENTINEL_AUTH_REMEMBER_DAYS=30
```

## Criar o administrador

Execute com o mesmo usuário do serviço para gravar no banco configurado:

```bash
sudo -u pisentinel /opt/pi-sentinel/.venv/bin/python /opt/pi-sentinel/scripts/manage_users.py create --username admin --must-change-password
```

A senha é solicitada sem aparecer no terminal. Ela não deve ser colocada no arquivo de ambiente. O banco guarda apenas PBKDF2-SHA256 com salt individual.

## Perfis

**Administrador** tem acesso aos recursos atuais do PiSentinel: inventário, IP/MAC, destinos, histórico, incidentes, câmera, arquivos, configurações, descoberta e controles já autorizados.

**Visitante** pode abrir somente:

- resumo de dispositivos respondendo, sem resposta e incidentes abertos;
- diagnóstico operacional resumido;
- cards das impressoras com estado, progresso, temperaturas, trabalho atual, ETA e última comunicação.

O backend bloqueia para visitantes o inventário, IP/MAC, destinos, incidentes detalhados, câmera, arquivos, histórico, parâmetros, configuração, descoberta e qualquer operação de escrita.

## Sessões e proteção

- token aleatório de 256 bits em cookie `HttpOnly` e `SameSite=Strict`;
- somente o hash SHA-256 do token é salvo no SQLite;
- sem **Lembrar de mim**, o navegador recebe um cookie de sessão e o servidor aplica a expiração absoluta configurável de 1 a 168 horas;
- com **Lembrar de mim**, o cookie e a sessão persistem pelo período configurável de 1 a 365 dias (30 dias por padrão);
- usuário e senha nunca são guardados pelo frontend em `localStorage` ou `sessionStorage`;
- cinco falhas de login bloqueiam novas tentativas por 15 minutos;
- mudança de senha revoga todas as sessões do usuário;
- requisições de escrita continuam exigindo JSON e validação de mesma origem.

Como a instalação local usa HTTP, o cookie não recebe `Secure`. Para expor o painel fora da LAN, use o Raspberry Pi Connect ou um proxy HTTPS confiável; não publique diretamente a porta 8090 na internet.
