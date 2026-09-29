# PiSentinel — direção visual

Conceito gerado com a ferramenta ImageGen integrada, antes do frontend: `D:\projetos\Raspberry\outputs\pi-sentinel-concept.png`. O arquivo é referência visual, não um ativo servido no projeto. Prompt: tela completa desktop do PiSentinel em português, navy/cyan, sidebar com quatro seções, faixa de métricas, inventário em tabela, qualidade com gráfico real, diagnóstico e incidentes; sem fotos ou decoração sem função.

## Sistema
- Fundo escuro `#0b1118`, superfícies `#121c27`, divisores `#263441`, texto `#eaf2f9`, secundário `#95a9bd`, ação `#42d6c1`.
- Tipografia de sistema Segoe UI/Inter, títulos 36/24/18 px, conteúdo 14 px, microtexto 12 px, monospace para endereços.
- Sidebar 216 px; conteúdo máximo 1500 px com margem 32 px. Faixa horizontal de métricas, painéis leves e tabelas com linhas divisórias. Raio 8 px.
- Ícones nativos SVG 20 px, traço 1.6; estados incluem texto e ponto colorido. Botão primário cyan com texto escuro, secundário com borda, links cyan.
- Em telas pequenas: navegação horizontal, métricas em duas colunas, painéis empilhados, tabelas com rolagem interna e diálogos contidos no viewport.
- Edição: diálogo acessível nativo com rótulos; dispositivo (nome/grupo/observações); alvo (nome, destino, tipo, rede, porta/DNS, habilitado). Confirmação inline antes de excluir alvo.

## Dados
Números, nomes e exemplos desenhados pelo ImageGen são conceituais, nunca dados de produção. A aplicação usa somente `/api`. Grupos reais: computadores, consoles, impressoras 3D, infraestrutura, visitantes e sem grupo. Falhas abrem lacunas nos gráficos; ausência de amostra não vira zero. Frescor do coletor sempre visível. Identificação da banda/SSID de cada dispositivo não é inferida.

## Referências técnicas
- https://vite.dev/guide/
- https://react.dev/reference/react/useEffect
