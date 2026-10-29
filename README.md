# Reunião Local

Grava reuniões presenciais, transcreve-as e gera um resumo com os pontos principais,
as decisões e as tarefas. Corre **toda no seu computador**: é gratuita, funciona sem
internet depois de instalada e o áudio nunca sai da máquina.

| Etapa       | Ferramenta                                  |
|-------------|---------------------------------------------|
| Gravação    | `sounddevice` (microfone do portátil ou USB) |
| Transcrição | `faster-whisper`, modelo `large-v3-turbo` na GPU |
| Resumo      | Ollama (em Docker) com o modelo `qwen3:8b`  |

## Instalação (uma vez)

Requer o Docker instalado e acessível sem `sudo` (utilizador no grupo `docker`).

```bash
cd meet-ai
chmod +x instalar.sh
./instalar.sh
```

O Ollama corre no contentor `reuniao-ollama` (ver `docker-compose.yml`) e não é
instalado na máquina. Com GPU NVIDIA, o instalador instala também o
`nvidia-container-toolkit` para o contentor usar a GPU.

Na primeira transcrição, o modelo Whisper (~1,6 GB) é descarregado automaticamente.

## Uso

```bash
reuniao gravar --titulo "Reunião com cliente"   # Ctrl+C para terminar
reuniao gravar --meet --titulo "Daily"          # reunião online (Meet, Teams, Zoom…)
reuniao processar ~/Downloads/audio.m4a         # áudio que já existe (WhatsApp, telemóvel…)
reuniao resumir ~/Reunioes/<pasta>/transcricao.txt   # refaz só o resumo
reuniao microfones                              # lista os microfones
reuniao gravar --dispositivo 3                  # usa um microfone específico
```

Cada reunião fica numa pasta própria dentro de `~/Reunioes/AAAA-MM-DD_HHMM_titulo/`:

- `audio.wav` — a gravação (é gravada no disco à medida que decorre, por isso não se perde se algo falhar)
- `transcricao.txt` — o texto, com os tempos
- `transcricao.json` — os segmentos, para usar noutras ferramentas
- `resumo.md` — resumo, pontos principais, decisões, tarefas e questões em aberto

## Opções úteis

| Opção | Para quê |
|-------|----------|
| `--modelo llama3.1:8b` | usar outro modelo do Ollama (instale primeiro com `docker exec reuniao-ollama ollama pull …`) |
| `--whisper medium` | usar um modelo Whisper mais leve |
| `--cpu` | não usar a GPU |
| `--idioma en` | reuniões noutra língua |
| `--sem-resumo` | só gravar e transcrever |

Os valores por defeito estão no topo de `reuniao.py`.

### Reuniões online (`--meet`)

Com `--meet`, além do microfone grava-se também o som que sai nas colunas/auscultadores —
ou seja, os outros participantes. Funciona com qualquer aplicação (Meet, Teams, Zoom, Discord…):
basta entrar na reunião no browser como habitualmente e correr o comando nesta máquina.
A app não entra na reunião nem aparece como participante; tal como com um gravador numa mesa,
convém avisar os presentes de que a reunião está a ser gravada.

Tecnicamente, cria-se um sink virtual no PulseAudio (`pactl`) que mistura a saída de som com o
microfone; é removido automaticamente ao terminar. Requer `pulseaudio-utils`.

## Desempenho esperado (RTX 4060, 8 GB)

- Transcrição: uma reunião de 1 hora demora aproximadamente 2 a 4 minutos.
- Resumo: entre 30 segundos e 2 minutos. As reuniões longas são divididas em partes e o resumo final junta-as.
- O Whisper e o Ollama usam a GPU um de cada vez, por isso os 8 GB de VRAM chegam.

## Problemas comuns

- **"GPU indisponível… A usar CPU"**: corra sempre pelo atalho `reuniao`, porque é ele que configura as bibliotecas CUDA. Confirme também que `nvidia-smi` funciona.
- **"O Ollama não está a correr"**: o atalho `reuniao` arranca o contentor sozinho; se falhar, corra `docker compose up -d` na pasta do projeto e veja `docker logs reuniao-ollama`.
- **Resumo muito lento**: o Ollama pode estar sem GPU. Confirme com `docker exec reuniao-ollama nvidia-smi` e que o `.env` inclui `docker-compose.gpu.yml`.
- **A gravação fica em silêncio**: corra `reuniao microfones`, escolha o microfone certo com `--dispositivo N` e verifique o volume de entrada nas definições de som.
- **Qualidade do áudio**: pouse o portátil no centro da mesa. Um microfone USB omnidirecional barato melhora muito o resultado.

## Ideias para evoluir

- **Identificar quem fala** (diarização): `pyannote.audio` é gratuito, mas o modelo exige aceitar os termos no Hugging Face.
- **Interface gráfica** com um botão de gravar/parar (Tkinter, ou uma página web local com FastAPI).
- **Notas importantes**: carregar numa tecla durante a gravação para marcar o momento.
