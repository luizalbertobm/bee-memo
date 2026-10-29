# Reunião Local

Grava reuniões — presenciais ou online (Meet, Teams, Zoom…) — ou processa um áudio que já
existe, como uma mensagem de voz do WhatsApp, e gera a transcrição — com **quem disse o quê** —
e um resumo com os pontos principais, as decisões e as tarefas. Corre **toda no seu
computador**: é gratuita, funciona sem internet depois de instalada e o áudio nunca sai da máquina.

| Etapa       | Ferramenta                                                     |
|-------------|----------------------------------------------------------------|
| Gravação    | `sounddevice` + PulseAudio (microfone e, no modo online, o som do sistema) |
| Conversão   | `ffmpeg` (para ficheiros: opus, mp3, wav, m4a, vídeo…)        |
| Transcrição | `faster-whisper`, modelo `large-v3-turbo` na GPU              |
| Quem fala   | `sherpa-onnx` (segmentação pyannote + vozes WeSpeaker), no CPU |
| Resumo      | Ollama (em Docker) com o modelo `qwen3:8b`                    |
| Interface   | GTK 3 (segue o tema do sistema)                               |

## Instalação (uma vez)

Requer Ubuntu, Zorin OS ou Debian, e o Docker instalado e acessível sem `sudo`
(utilizador no grupo `docker`).

```bash
cd meet-ai
chmod +x instalar.sh
./instalar.sh
```

O instalador:

- instala os pacotes do sistema (`ffmpeg`, PortAudio, GTK para Python);
- cria o ambiente Python em `.venv`;
- com GPU NVIDIA, instala as bibliotecas CUDA do Whisper e o `nvidia-container-toolkit`,
  para o Ollama usar a GPU dentro do Docker;
- arranca o Ollama no contentor `reuniao-ollama` e descarrega o modelo `qwen3:8b`;
- cria o comando `reuniao` em `~/.local/bin` e o atalho **Reunião Local** no menu de aplicações.

Na primeira transcrição, o modelo Whisper (~1,6 GB) é descarregado automaticamente, e na
primeira identificação de oradores os modelos do sherpa-onnx (~33 MB, para
`~/.cache/reuniao/modelos`).

## Interface gráfica

```bash
reuniao            # ou "Reunião Local" no menu de aplicações
```

### Gravar uma reunião

1. Escreva um **título** (opcional, é usado no nome da pasta).
2. Escolha o **modo**:
   - **Ao vivo** — grava só o microfone. Para reuniões presenciais.
   - **Online** — grava o microfone e o som do sistema, ou seja, os outros participantes.
     Entre na reunião no browser como habitualmente; ver [Reuniões online](#reuniões-online).
3. Escolha o **microfone** (aparecem com os mesmos nomes das Definições de som) e se a
   transcrição usa a **GPU**.
4. **Identificar quem fala** vem ligado: a transcrição distingue as vozes (Pessoa 1, 2…).
   Se souber quantas pessoas vão falar, indique-o em **Número de pessoas** — a separação
   fica mais fiável. Ver [Quem fala](#quem-fala).
5. Carregue em **Iniciar gravação** e, no fim, em **Terminar reunião**.

### Processar um ficheiro de áudio

Para um áudio que já existe — uma mensagem de voz do WhatsApp (`.opus`), um `.mp3`, `.wav`,
`.m4a` ou até um vídeo — use **Processar um ficheiro de áudio…**, por baixo do botão de
gravar, ou **arraste o ficheiro para a janela**. Passa pelo mesmo processo: transcrição e resumo.

### Resultado

Durante o processamento, a barra de título mostra a etapa atual (*A transcrever…*,
*A gerar o resumo…*) e o painel mostra o progresso. No fim, o painel mostra o **resumo** e
aparecem os botões:

| Botão | O que faz |
|-------|-----------|
| **Resumo** | mostra o `resumo.md` no painel |
| **Transcrição** | mostra o `transcricao.txt` no painel, com os tempos e o nome de quem fala a negrito |
| **Copiar** | copia para a área de transferência o texto que está no painel |
| **Áudio** | abre a gravação no leitor de áudio do sistema |

O ícone de pasta na barra de título abre a pasta da reunião. Se algo falhar, o painel
mantém o registo, para se perceber o que aconteceu, e os ficheiros já gerados continuam
acessíveis.

Quando o resumo fica pronto com a janela em segundo plano, aparece uma notificação do sistema.
As escolhas (modo, microfone, GPU, identificar quem fala) ficam guardadas em
`~/.config/reuniao/interface.json`.

A janela é só uma frente para os comandos abaixo: tudo o que faz também se faz no terminal.

## Linha de comandos

```bash
reuniao gravar --titulo "Reunião com cliente"   # Ctrl+C para terminar
reuniao gravar --meet --titulo "Daily"          # reunião online (Meet, Teams, Zoom…)
reuniao processar ~/Downloads/audio.opus        # áudio que já existe (WhatsApp, telemóvel…)
reuniao resumir ~/Reunioes/<pasta>/transcricao.txt   # refaz só o resumo
reuniao microfones                              # lista os microfones
reuniao gravar --dispositivo 3                  # usa um microfone específico
reuniao                                         # abre a interface gráfica
```

O `--dispositivo` aceita o número mostrado por `reuniao microfones` ou o nome de uma fonte
do PulseAudio (`pactl list short sources`).

### Ficheiros gerados

Cada reunião fica numa pasta própria em `~/Reunioes/AAAA-MM-DD_HHMM_titulo/`:

- `audio.wav` — a gravação, em WAV 16 kHz mono. É escrita no disco à medida que decorre,
  por isso não se perde se algo falhar.
- `transcricao.txt` — o texto, com os tempos e quem fala (`[00:05] Pessoa 2: …`).
- `transcricao.json` — os segmentos (início, fim, texto e orador), para usar noutras ferramentas.
- `resumo.md` — resumo, pontos principais, decisões, tarefas e questões em aberto.

### Opções úteis

| Opção | Para quê |
|-------|----------|
| `--modelo llama3.1:8b` | usar outro modelo do Ollama (instale primeiro com `docker exec reuniao-ollama ollama pull …`) |
| `--whisper medium` | usar um modelo Whisper mais leve |
| `--cpu` | não usar a GPU |
| `--idioma en` | reuniões noutra língua |
| `--sem-resumo` | só gravar e transcrever |
| `--pessoas 3` | quantas pessoas falam, se souber (por defeito é detetado sozinho) |
| `--sem-oradores` | não identificar quem fala |
| `--saida <pasta>` | guardar as reuniões noutra pasta |

As opções podem vir antes ou depois do comando (`reuniao --cpu gravar` ou
`reuniao gravar --cpu`). Os valores por defeito estão no topo de `reuniao.py`.

## Reuniões online

No modo online (`--meet` no terminal), além do microfone grava-se também o som que sai nas
colunas ou nos auscultadores, ou seja, os outros participantes. Funciona com qualquer
aplicação (Meet, Teams, Zoom, Discord…): basta entrar na reunião como habitualmente, nesta
máquina, e começar a gravar.

A app **não entra na reunião** nem aparece como participante. Tal como com um gravador numa
mesa, convém avisar os presentes de que a reunião está a ser gravada.

Tecnicamente, cria-se um sink virtual no PulseAudio (`pactl`) que mistura a saída de som com o
microfone; é removido automaticamente ao terminar. Com colunas em vez de auscultadores, o
microfone também apanha o som dos outros, o que pode duplicar algumas frases na transcrição.

## Quem fala

Depois da transcrição, a app identifica as diferentes vozes (*diarização*) e marca cada fala
com **Pessoa 1, Pessoa 2…**, pela ordem em que cada uma fala pela primeira vez:

```
[00:05] Pessoa 2: Eu proponho lançar na próxima sexta-feira.
[00:11] Pessoa 1: Concordo. Quem prepara as notas de lançamento?
[00:18] Pessoa 2: Eu trato das notas de lançamento até quinta-feira.
```

O resumo usa esta informação para dizer quem decidiu ou ficou responsável por cada tarefa
(`- [ ] Preparar as notas de lançamento — Pessoa 2 — prazo: quinta-feira`). Se só houver uma
voz, como numa nota de voz do WhatsApp, a transcrição fica sem rótulos.

Como funciona: um modelo deteta onde há fala e onde a voz muda, outro gera uma "impressão
digital" de cada trecho, e os trechos parecidos são agrupados na mesma pessoa. Depois, cada
palavra da transcrição fica com a pessoa que estava a falar naquele instante — por isso uma
troca rápida de vozes dentro da mesma frase também fica separada.

Limites:

- É uma estimativa: vozes muito parecidas podem ser juntadas, e uma pessoa com a voz a
  mudar muito (riso, distância ao microfone) pode aparecer como duas. Indicar o **número de
  pessoas** (`--pessoas N`) resolve a maior parte destes casos.
- Falas sobrepostas são atribuídas a quem fala mais naquele instante.
- Os nomes são genéricos; os rótulos não ficam associados a pessoas entre reuniões.
- No modo online com colunas, o microfone também apanha os outros participantes, o que pode
  baralhar a separação. Use auscultadores.

## Desempenho esperado (RTX 4060, 8 GB)

- Transcrição: uma reunião de 1 hora demora aproximadamente 2 a 4 minutos.
- Quem fala: cerca de 6 a 7 minutos por hora de reunião, no CPU. Desligue-o
  (`--sem-oradores` ou na janela) quando não precisar.
- Resumo: entre 30 segundos e 2 minutos. As reuniões longas são divididas em partes e o
  resumo final junta-as.
- O Whisper e o Ollama usam a GPU um de cada vez: o Whisper é libertado antes do resumo e o
  Ollama descarrega o modelo logo a seguir, por isso os 8 GB de VRAM chegam.

## Problemas comuns

- **"GPU indisponível… A usar CPU"**: corra sempre pelo atalho `reuniao`, porque é ele que
  configura as bibliotecas CUDA. Confirme também que `nvidia-smi` funciona. Se o erro for
  *out of memory*, outro programa está a ocupar a VRAM; veja com `nvidia-smi`.
- **"O Ollama não está a correr"**: o atalho `reuniao` arranca o contentor sozinho; se falhar,
  corra `docker compose up -d` na pasta do projeto e veja `docker logs reuniao-ollama`.
- **Resumo muito lento**: o Ollama pode estar sem GPU. Confirme com
  `docker exec reuniao-ollama nvidia-smi` e que o `.env` inclui `docker-compose.gpu.yml`.
- **A janela não abre**: confirme que o GTK para Python está instalado
  (`sudo apt install python3-gi gir1.2-gtk-3.0`).
- **A gravação fica em silêncio**: escolha outro microfone (na janela, ou com
  `reuniao microfones` e `--dispositivo`) e verifique o volume de entrada nas definições de som.
- **"Não foi possível ler o áudio"**: o ficheiro não é um áudio válido ou está corrompido.
- **Pessoas a mais ou a menos na transcrição**: indique o número de pessoas (`--pessoas N`
  ou o campo na janela). Para ajustar o modo automático, mude `LIMIAR_ORADORES` no topo de
  `reuniao.py` (mais baixo separa mais vozes).
- **"Não foi possível descarregar" os modelos de oradores**: a primeira identificação de
  oradores precisa de internet. Sem ela, desligue-a com `--sem-oradores`.
- **Aviso sobre `HF_TOKEN`**: é só um aviso do Hugging Face ao verificar o modelo Whisper; pode
  ser ignorado.
- **Qualidade do áudio**: pouse o portátil no centro da mesa. Um microfone USB omnidirecional
  barato melhora muito o resultado.

## Estrutura do projeto

| Ficheiro | Para quê |
|----------|----------|
| `reuniao.py` | gravação, conversão, transcrição, resumo e linha de comandos |
| `interface.py` | janela GTK; lança o `reuniao.py` e mostra o resultado |
| `reuniao` | atalho que prepara as bibliotecas CUDA, arranca o Ollama e chama o `reuniao.py` (gerado pelo instalador) |
| `reuniao.desktop` | modelo do atalho no menu de aplicações |
| `instalar.sh` | instalação completa |
| `docker-compose.yml`, `docker-compose.gpu.yml` | contentor do Ollama, com e sem GPU |

## Ideias para evoluir

- **Dar nomes às pessoas**: trocar Pessoa 1, 2… por nomes reais e refazer o resumo.
- **Quem fala na GPU**: o sherpa-onnx também corre em CUDA, o que reduziria os 6 a 7 minutos
  por hora de reunião.
- **Notas importantes**: carregar numa tecla durante a gravação para marcar o momento.
