# GifConverter

Converte GIFs para uma taxa de quadros maior (ex.: **60fps**) gerando os
quadros intermediarios por interpolacao (FFmpeg `minterpolate` ou IA
**RIFE**), com opcao de **upscaling** da resolucao (Lanczos ou IA
**Real-ESRGAN**) e saida em GIF/MP4/WebM. Interface grafica com **arrastar
e soltar**.

## Como funciona

Ha dois motores de **interpolacao** (opcao **Interpolar**, ou `-I`) que
geram os quadros intermediarios para aumentar o fps:

### 1. FFmpeg (minterpolate) — padrao, rapido

Analisa o movimento entre quadros vizinhos e sintetiza novos quadros no
meio. Qualidade da interpolacao:
- **alta**: `mi_mode=mci` (compensacao de movimento) — melhor fluidez.
- **rapida**: `mi_mode=blend` (mistura de quadros) — mais rapida.

### 2. RIFE (IA) — melhor fluidez

Rede neural (Real-Time Intermediate Flow Estimation) que **sintetiza os
quadros intermediarios com muito mais qualidade**, especialmente em
movimentos rapidos/complexos. Fluxo: extrai os quadros originais -> a IA
gera os intermediarios ate o fps alvo -> remonta. Roda na GPU via
**Vulkan**; e mais lento que o FFmpeg.

> Os dois motores de interpolacao podem ser combinados com os dois motores
> de upscaling (ex.: RIFE + Real-ESRGAN = fluidez e resolucao no maximo).

Para GIF, uma paleta dedicada e aplicada no final para preservar as cores.

## Binarios nao versionados

Para manter o repositorio leve, os binarios grandes **nao** ficam no git
(veja `.gitignore`). Apos clonar, baixe-os:

- **FFmpeg** (`ffmpeg.exe`) na raiz do projeto — build "full" de
  <https://www.gyan.dev/ffmpeg/builds/> (ou use o do PATH).
- **Real-ESRGAN** — pasta `realesrgan/` (binario `realesrgan-ncnn-vulkan.exe`
  + `models/`) de <https://github.com/xinntao/Real-ESRGAN/releases>.
- **RIFE** — pasta `rife/` (binario `rife-ncnn-vulkan.exe` + modelo
  `rife-v4.6/`) de <https://github.com/nihui/rife-ncnn-vulkan/releases>.

Sem eles: o modo classico ainda funciona se o FFmpeg estiver no PATH; os
modos de IA (RIFE/Real-ESRGAN) exigem as respectivas pastas.

## Pre-requisitos

1. **FFmpeg** no PATH.
   - Windows: baixe a build "full" em <https://www.gyan.dev/ffmpeg/builds/>
     e adicione a pasta `bin` ao PATH.
   - Teste com: `ffmpeg -version`
2. **Python 3.9+** (testado no 3.14).

## Instalacao

```bash
pip install -r requirements.txt
```

> O pacote `tkinterdnd2` habilita o arrastar-e-soltar. Se voce nao
> instalar, o app ainda funciona clicando na area para escolher o arquivo.

## Uso

### Interface grafica

```bash
python app.py
```

Arraste um `.gif` **ou um video** (MP4, WebM, MOV, MKV, AVI...) para a
janela (ou clique para escolher), selecione o FPS e a qualidade, e clique
em **Converter**. O resultado e salvo na mesma pasta como
`nome_60fps.<formato>`.

> **MP4 -> GIF:** basta arrastar o video e escolher **Formato: gif** (ja
> selecionado automaticamente ao carregar um video). Da tambem para o
> caminho inverso, GIF -> MP4/WebM.

### Linha de comando

```bash
python converter.py entrada.gif                      # 60fps, MP4 (padrao)
python converter.py entrada.gif -f webm              # 60fps em WebM
python converter.py entrada.gif -f gif               # GIF (limitado a 50fps)
python converter.py entrada.gif --fps 120 -f mp4     # 120fps em MP4
python converter.py entrada.gif -u 2                 # 2x classico (Lanczos)
python converter.py entrada.gif -f gif -u 4          # GIF 50fps, 4x classico
python converter.py entrada.gif -u 4 -e ia           # 4x com IA (Real-ESRGAN)
python converter.py entrada.gif -u 2 -e ia -m anime  # 2x IA, modelo anime
python converter.py entrada.gif -I rife              # interpolacao por IA (RIFE)
python converter.py entrada.gif -I rife -e ia -u 2   # RIFE + upscale IA 2x
python converter.py video.mp4 -f gif --fps 50        # MP4 -> GIF
python converter.py video.mp4 -f gif -e ia -u 2      # MP4 -> GIF com upscale IA
python converter.py video.mp4 -f gif --start 5 --duration 3   # trecho 5s..8s
python converter.py video.mp4 -f gif --max-width 480          # limita largura
```

## Corte e largura maxima

Uteis principalmente para **MP4 -> GIF** (GIF longo/grande fica pesado):

- **Inicio (s)** / `--start`: segundo onde comeca o corte (padrao 0).
- **Duracao (s)** / `--duration`: quantos segundos converter (vazio = ate o fim).
- **Largura max** / `--max-width`: reduz a largura para no maximo esse valor
  (nunca amplia), mantendo a proporcao. Otimo para deixar o GIF leve.

> A entrada tambem pode ser video (MP4/WebM/MOV/MKV/AVI), nao so GIF.

## Upscaling (melhorar a resolucao)

Ha dois motores de upscaling (opcao **Motor** na interface, ou `-e`):

### 1. Classico (Lanczos + nitidez) — padrao, rapido

Amplia com o algoritmo **Lanczos** + realce de **nitidez** (`unsharp`).
Deixa a imagem maior e mais nitida, mas **nao inventa detalhes novos** —
so reamostra o que ja existe. Aceita qualquer fator (2x, 3x, 4x). Roda em
uma unica passada do FFmpeg, bem rapido.

### 2. IA (Real-ESRGAN) — melhor qualidade

Super-resolucao por rede neural: **reconstroi detalhes de verdade**, otimo
para desenho/anime/GIF/pixel art. Fluxo em 3 etapas: interpola -> extrai
os quadros -> a IA amplia cada quadro -> remonta. Aceita **2x, 3x ou 4x**.

Roda na GPU via **Vulkan** (funciona ate em placa integrada); e mais lento
que o classico, especialmente em GIFs longos ou em 4x.

Modelos (opcao **Modelo IA**, ou `-m`):

| Modelo       | Escalas | Melhor para |
|--------------|---------|-------------|
| `anime`      | 2/3/4x  | Desenho, anime, GIF, pixel art (recomendado) |
| `foto`       | 4x      | Fotos reais |
| `ilustracao` | 4x      | Ilustracao/anime detalhada |

> Os modelos `foto` e `ilustracao` so fazem 4x; se pedir outra escala com
> eles, o programa usa 4x automaticamente.

O Real-ESRGAN ja vem embutido no `.exe`. Rodando pelo codigo-fonte, ele
espera a pasta `realesrgan/` (binario + `models/`) ao lado dos scripts.

## Executavel (.exe)

Ja existe um executavel pronto em `dist\GifConverter.exe` — basta dar
duplo clique, sem precisar de Python instalado.

> **100% portatil:** FFmpeg, Real-ESRGAN **e** RIFE estao **embutidos
> dentro do `.exe`**. Da para copiar o arquivo para qualquer PC Windows e
> rodar, sem instalar nada. Por isso ele tem ~154 MB. (O app ainda usa o
> FFmpeg do PATH como alternativa, caso o embutido nao esteja disponivel.)

### Como gerar o `.exe` de novo (apos alterar o codigo)

O `--add-binary` precisa de um `ffmpeg.exe` na pasta do projeto (ja existe
uma copia aqui). Se precisar recopiar, pegue de uma build "full" do FFmpeg.

```bash
pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name GifConverter \
  --collect-all tkinterdnd2 \
  --add-binary "ffmpeg.exe;." \
  --add-data "realesrgan;realesrgan" \
  --add-data "rife;rife" \
  app.py
```

O resultado fica em `dist\GifConverter.exe` (~154 MB: FFmpeg + Real-ESRGAN +
RIFE embutidos). Feche o `.exe` antes de reconstruir, senao o PyInstaller
nao consegue sobrescrever o arquivo.

## Formatos de saida (importante!)

O formato GIF guarda o tempo de cada quadro em **centesimos de segundo**
(inteiros). A 60fps o tempo por quadro seria ~1,67cs, e os players tratam
delays abaixo de 2cs como 10cs — resultado: **o GIF toca em camera lenta**.

Por isso:

| Formato | FPS real | Observacao |
|---------|----------|-----------|
| **mp4**  | 60fps (ou mais) | Recomendado. 60fps de verdade, velocidade certa, arquivo menor. |
| **webm** | 60fps (ou mais) | Igual ao mp4, com codec VP9. |
| **gif**  | ate ~50fps | Limitado automaticamente a 50fps para nao ficar lento. |

Se voce escolher GIF e pedir mais de 50fps, o programa **limita a 50fps** e
avisa. Para 60fps reais, use **mp4** ou **webm**.

## Observacoes

- GIFs sao limitados a 256 cores; aumentar o FPS deixa o arquivo maior.
- Interpolar acima do movimento real (ex.: de 5fps para 60fps) pode gerar
  leves artefatos em cenas com muito movimento — teste a qualidade "rapida".
