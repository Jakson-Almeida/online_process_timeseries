# Online Process Timeseries

Software para acompanhar, em tempo real, o comportamento de **sensores ópticos em fibra** — pequenos dispositivos que respondem a mudanças de temperatura, deformação, pressão ou outros fenômenos físicos alterando a luz que passam por eles.

Desenvolvido pela **LiTel**, o programa lê os espectros de luz captados por equipamentos de laboratório e transforma essas leituras em gráficos fáceis de interpretar, permitindo ver como um sensor se comporta ao longo do tempo.

---

## Sobre o software

Este software:

- **Conecta-se** ao aparelho de leitura (interrogador ou analisador de espectro)
- **Captura** leituras de luz de forma contínua ou sob demanda
- **Identifica** automaticamente o ponto de interesse no espectro (um vale ou um pico, conforme o tipo de sensor)
- **Mostra gráficos** da evolução dessa medida ao longo do tempo
- **Permite salvar** os dados para análise posterior

Em resumo: é uma ferramenta de **monitoramento e acompanhamento** de sensores ópticos, pensada para uso em laboratório ou em bancadas de teste.

---

## Tipos de sensor suportados

O programa trabalha com três categorias de fibra óptica:

| Tipo | O que acompanha |
|------|-----------------|
| **LPG** | Gratings de período longo — busca o **vale** (mínimo) no espectro |
| **FBG** | Gratings de Bragg em fibra — busca **picos** no espectro |
| **Interferômetro** | Sensores interferométricos — também baseados em picos |

O usuário escolhe o tipo na tela inicial; o software adapta a análise automaticamente.

---

## Como funciona na prática

1. **Configuração** — Na janela inicial, escolhe-se o equipamento de leitura, a faixa de comprimento de onda de interesse, os canais a monitorar e o tipo de fibra.
2. **Análise** — Abre-se a janela de análise, onde os espectros aparecem em tempo real.
3. **Região de interesse** — É possível delimitar no gráfico a faixa onde o software deve procurar a medida relevante.
4. **Acompanhamento temporal** — Um segundo gráfico mostra como a leitura evolui segundo a segundo (ou conforme a taxa de amostragem configurada).
5. **Gravação** — Os dados podem ser exportados em arquivo para consulta ou processamento futuro.

---

## Equipamentos compatíveis

O software se comunica com interrogadores e analisadores de espectro de diferentes fabricantes:

- **IBSEN IMON-512** (porta serial)
- **BraggMeter FS22DI** (rede TCP/IP)
- **Thorlabs CCT11** e **OSA203** (analizadores de espectro óptico)

Alguns modelos exigem drivers ou bibliotecas adicionais fornecidos pelo fabricante, que não fazem parte deste repositório.

---

## Como executar

**Requisitos:** Python 3.10 ou superior e Windows.

```powershell
# Criar ambiente virtual (recomendado)
python -m venv .venv
.\.venv\Scripts\activate

# Instalar dependências
pip install -r requirements.txt

# Iniciar o programa
python main.py
```

> **Dica:** Se a instalação do PySide6 falhar por erro de caminho longo no Windows, use um ambiente virtual na pasta do projeto (como acima) ou habilite o suporte a caminhos longos nas configurações do sistema.

---

## Observações

- O programa **precisa do hardware conectado** para capturar dados reais. Sem o equipamento, a interface abre normalmente, mas a conexão com o sensor falhará.
- As preferências de configuração (faixa de onda, tipo de fibra, tema claro/escuro etc.) são lembradas entre sessões.
- Para equipamentos Thorlabs, é necessário instalar separadamente as DLLs do SDK correspondente.

---

## Licença

Este projeto está licenciado sob a [MIT License](LICENSE).
