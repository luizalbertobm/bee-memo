#!/usr/bin/env python3
"""
Reunião Local — grava uma reunião presencial, transcreve e gera um resumo.
100% local e gratuito: faster-whisper (transcrição) + Ollama (resumo).

Uso:
  python reuniao.py                        # abre a interface gráfica
  python reuniao.py gravar                 # grava até carregar Ctrl+C, depois transcreve e resume
  python reuniao.py gravar --meet          # reunião online: grava o microfone e o som do sistema
  python reuniao.py processar audio.m4a    # processa um áudio que já existe
  python reuniao.py resumir transcricao.txt  # só refaz o resumo a partir de uma transcrição
"""

import argparse
import contextlib
import datetime as dt
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

# ----------------------------------------------------------------------------
# Configuração (pode alterar aqui ou por argumentos na linha de comandos)
# ----------------------------------------------------------------------------
PASTA_SAIDA = Path.home() / "Reunioes"
MODELO_WHISPER = "large-v3-turbo"      # GPU: large-v3-turbo | CPU: small ou medium
IDIOMA = "pt"
MODELO_OLLAMA = "qwen3:8b"             # alternativas: llama3.1:8b, gemma3:4b
OLLAMA_URL = "http://localhost:11434"
TAXA_AMOSTRAGEM = 16000                # 16 kHz mono é o que o Whisper usa
TAMANHO_BLOCO = 42000                  # caracteres por chamada ao LLM (~40 min de reunião)

# Identificação de quem fala (diarização) com sherpa-onnx; os modelos descarregam-se na
# primeira utilização para PASTA_MODELOS.
PASTA_MODELOS = Path.home() / ".cache" / "reuniao" / "modelos"
URL_MODELOS = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
MODELO_SEGMENTACAO = "sherpa-onnx-pyannote-segmentation-3-0"   # onde há fala e mudanças de voz
MODELO_VOZES = "wespeaker_en_voxceleb_resnet34_LM.onnx"           # "impressão digital" de cada voz
LIMIAR_ORADORES = 0.7                  # sem --pessoas: mais baixo → separa mais vozes


# ----------------------------------------------------------------------------
# 1. Gravação
# ----------------------------------------------------------------------------
def gravar(destino: Path, dispositivo=None) -> Path:
    import sounddevice as sd
    import soundfile as sf

    fila: "queue.Queue" = queue.Queue()

    def callback(dados, frames, tempo, estado):
        if estado:
            print(f"\n[aviso] {estado}", file=sys.stderr)
        fila.put(dados.copy())

    print(f"🎙️  A gravar para {destino}")
    print("    Carregue Ctrl+C para terminar a reunião.\n")
    inicio = dt.datetime.now()

    # Um `kill` (ou fechar o terminal) termina como o Ctrl+C: fecha o WAV e limpa o áudio.
    def terminar(sinal, frame):
        raise KeyboardInterrupt
    for sinal in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sinal, terminar)

    # Escreve no disco à medida que grava: se algo falhar, o áudio não se perde.
    with sf.SoundFile(destino, mode="w", samplerate=TAXA_AMOSTRAGEM,
                      channels=1, subtype="PCM_16") as ficheiro:
        with sd.InputStream(samplerate=TAXA_AMOSTRAGEM, channels=1,
                            dtype="int16", device=dispositivo, callback=callback):
            try:
                while True:
                    ficheiro.write(fila.get())
                    duracao = dt.datetime.now() - inicio
                    print(f"\r    ⏺  {str(duracao).split('.')[0]}", end="", flush=True)
            except KeyboardInterrupt:
                pass
        while not fila.empty():
            ficheiro.write(fila.get())

    print(f"\n✅ Gravação terminada ({str(dt.datetime.now() - inicio).split('.')[0]}).\n")
    return destino


def listar_dispositivos():
    import sounddevice as sd
    print(sd.query_devices())


def pactl(*argumentos: str) -> str:
    return subprocess.run(["pactl", *argumentos], check=True, capture_output=True,
                          text=True).stdout.strip()


def fontes_pulse() -> list[str]:
    """Nomes das fontes de áudio do PulseAudio (microfones e monitores)."""
    if not shutil.which("pactl"):
        return []
    return [linha.split("\t")[1] for linha in pactl("list", "short", "sources").splitlines()]


@contextlib.contextmanager
def capturar_sistema(microfone: str | None = None):
    """Mistura o som do sistema (os outros participantes) com o microfone (nós).

    Cria um sink virtual no PulseAudio e despeja nele o 'monitor' da saída de
    som e o microfone; a gravação lê desse sink. No fim desfaz tudo.
    """
    if not shutil.which("pactl"):
        sys.exit("Erro: o pactl não está instalado (sudo apt install pulseaudio-utils).")
    colunas = pactl("get-default-sink")
    microfone = microfone or pactl("get-default-source")
    modulos = []
    try:
        modulos.append(pactl("load-module", "module-null-sink", "sink_name=reuniao_mix",
                             "sink_properties=device.description=Reuniao"))
        for fonte in (f"{colunas}.monitor", microfone):
            modulos.append(pactl("load-module", "module-loopback", f"source={fonte}",
                                 "sink=reuniao_mix", "latency_msec=50"))
        # O libpulse lê esta variável quando o fluxo de gravação é aberto.
        os.environ["PULSE_SOURCE"] = "reuniao_mix.monitor"
        print("🔊 A captar também o som do sistema (reunião online).")
        yield
    finally:
        os.environ.pop("PULSE_SOURCE", None)
        for modulo in reversed(modulos):
            subprocess.run(["pactl", "unload-module", modulo], check=False)


# ----------------------------------------------------------------------------
# 2. Transcrição
# ----------------------------------------------------------------------------
def para_wav(origem: Path, destino: Path) -> Path:
    """Converte qualquer áudio/vídeo (m4a, mp3, ogg, mp4...) para WAV 16 kHz mono."""
    if not shutil.which("ffmpeg"):
        sys.exit("Erro: o ffmpeg não está instalado (sudo apt install ffmpeg).")
    resultado = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(origem),
                                "-vn", "-ar", str(TAXA_AMOSTRAGEM), "-ac", "1", str(destino)],
                               capture_output=True, text=True)
    if resultado.returncode != 0:
        motivo = (resultado.stderr.strip().splitlines() or ["formato desconhecido"])[-1]
        motivo = motivo.removeprefix(f"{origem}: ")     # o ffmpeg repete o caminho
        destino.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            destino.parent.rmdir()                       # não deixa uma pasta vazia
        sys.exit(f"Erro: não foi possível ler o áudio '{origem.name}' ({motivo}).")
    return destino


def carregar_modelo(nome: str, forcar_cpu: bool):
    from faster_whisper import WhisperModel

    if not forcar_cpu:
        try:
            modelo = WhisperModel(nome, device="cuda", compute_type="float16")
            print(f"🧠 Whisper '{nome}' na GPU.")
            return modelo
        except Exception as erro:  # sem CUDA/cuDNN → usa CPU
            print(f"[aviso] GPU indisponível ({erro.__class__.__name__}: {erro}). A usar CPU.")
    nome_cpu = "small" if nome.startswith("large") else nome
    print(f"🧠 Whisper '{nome_cpu}' no CPU (mais lento).")
    return WhisperModel(nome_cpu, device="cpu", compute_type="int8")


def transcrever(wav: Path, nome_modelo: str, idioma: str, forcar_cpu: bool,
                palavras: bool = False) -> list[dict]:
    """Segmentos {inicio, fim, texto}; com palavras=True, também os tempos de cada palavra
    (precisos para saber quem disse o quê quando duas pessoas falam no mesmo segmento)."""
    import numpy as np
    import soundfile as sf

    audio, taxa = sf.read(wav, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if taxa != TAXA_AMOSTRAGEM:
        sys.exit(f"Erro: o WAV tem {taxa} Hz; esperado {TAXA_AMOSTRAGEM} Hz.")

    modelo = carregar_modelo(nome_modelo, forcar_cpu)
    total = len(audio) / TAXA_AMOSTRAGEM
    print(f"📝 A transcrever {formatar_tempo(total)} de áudio...")

    segmentos, _ = modelo.transcribe(
        np.ascontiguousarray(audio), language=idioma, beam_size=5,
        vad_filter=True,                       # ignora silêncios
        vad_parameters={"min_silence_duration_ms": 500},
        word_timestamps=palavras,
    )
    resultado = []
    for s in segmentos:
        segmento = {"inicio": s.start, "fim": s.end, "texto": s.text.strip()}
        if palavras:
            segmento["palavras"] = [(p.start, p.end, p.word) for p in s.words or []]
        resultado.append(segmento)
        print(f"\r    {s.end / total:5.0%}", end="", flush=True)
    print("\r    100%\n")
    del segmentos, modelo                      # liberta a VRAM antes do Ollama carregar o LLM
    return resultado


def formatar_tempo(segundos: float) -> str:
    h, resto = divmod(int(segundos), 3600)
    m, s = divmod(resto, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def guardar_transcricao(segmentos: list[dict], pasta: Path) -> str:
    def fala(s):
        return f"{s['orador']}: {s['texto']}" if "orador" in s else s["texto"]
    com_tempos = "\n".join(f"[{formatar_tempo(s['inicio'])}] {fala(s)}" for s in segmentos)
    (pasta / "transcricao.txt").write_text(com_tempos + "\n", encoding="utf-8")
    (pasta / "transcricao.json").write_text(
        json.dumps(segmentos, ensure_ascii=False, indent=2), encoding="utf-8")
    return texto_para_resumo(segmentos)


def texto_para_resumo(segmentos: list[dict]) -> str:
    """Texto que vai para o LLM, com o momento de cada linha ("[12:30] …"), para o modelo
    saber quanto tempo se falou de cada assunto: uma linha por vez que alguém fala ou,
    sem oradores, uma linha a cada ~30 s."""
    com_oradores = any("orador" in s for s in segmentos)
    linhas: list[list] = []                            # [início, orador, texto]
    for s in segmentos:
        if linhas and (s.get("orador") == linhas[-1][1] if com_oradores
                       else s["inicio"] - linhas[-1][0] < 30):
            linhas[-1][2] += " " + s["texto"]
        else:
            linhas.append([s["inicio"], s.get("orador"), s["texto"]])
    return "\n".join(f"[{formatar_tempo(inicio)}] " + (f"{orador}: " if orador else "") + texto
                     for inicio, orador, texto in linhas)


def segundos(tempo: str) -> float:
    """"12:30" ou "1:02:03" → segundos."""
    total = 0.0
    for parte in tempo.split(":"):
        total = total * 60 + float(parte)
    return total


# ----------------------------------------------------------------------------
# 2b. Quem fala (diarização)
# ----------------------------------------------------------------------------
def descarregar(url: str, destino: Path) -> None:
    # Para um ficheiro temporário: um download interrompido não deixa um modelo corrompido.
    temporario = destino.with_name(destino.name + ".parcial")
    try:
        urllib.request.urlretrieve(url, temporario)
    except (urllib.error.URLError, OSError) as erro:
        temporario.unlink(missing_ok=True)
        sys.exit(f"Erro: não foi possível descarregar {url.rsplit('/', 1)[1]} ({erro}). "
                 "A identificação de oradores precisa de internet na primeira utilização; "
                 "pode desligá-la com --sem-oradores.")
    temporario.rename(destino)


def modelos_oradores() -> tuple[Path, Path]:
    segmentacao = PASTA_MODELOS / MODELO_SEGMENTACAO / "model.onnx"
    vozes = PASTA_MODELOS / MODELO_VOZES
    if segmentacao.exists() and vozes.exists():
        return segmentacao, vozes
    print("⬇️  A descarregar os modelos de identificação de oradores (~33 MB, só da primeira vez)...")
    PASTA_MODELOS.mkdir(parents=True, exist_ok=True)
    if not segmentacao.exists():
        arquivo = PASTA_MODELOS / f"{MODELO_SEGMENTACAO}.tar.bz2"
        descarregar(f"{URL_MODELOS}/speaker-segmentation-models/{arquivo.name}", arquivo)
        temporaria = PASTA_MODELOS / ".a-extrair"
        shutil.rmtree(temporaria, ignore_errors=True)
        with tarfile.open(arquivo) as tar:
            tar.extractall(temporaria, filter="data")
        shutil.rmtree(segmentacao.parent, ignore_errors=True)
        (temporaria / MODELO_SEGMENTACAO).rename(segmentacao.parent)
        shutil.rmtree(temporaria)
        arquivo.unlink()
    if not vozes.exists():
        # "recongition" (sic) é o nome da release no GitHub do sherpa-onnx
        descarregar(f"{URL_MODELOS}/speaker-recongition-models/{MODELO_VOZES}", vozes)
    return segmentacao, vozes


def diarizar(wav: Path, pessoas: int | None) -> list[tuple[float, float, int]]:
    """Turnos (início, fim, orador) de cada voz; pessoas=None deteta quantas são."""
    import sherpa_onnx
    import soundfile as sf

    segmentacao, vozes = modelos_oradores()
    print("👥 A identificar quem fala...")
    fios = max(1, (os.cpu_count() or 2) // 2)
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(segmentacao)),
            num_threads=fios),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(vozes),
                                                              num_threads=fios),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=pessoas or -1,
                                                    threshold=LIMIAR_ORADORES),
        min_duration_on=0.3, min_duration_off=0.5)
    diarizador = sherpa_onnx.OfflineSpeakerDiarization(config)

    audio, _ = sf.read(wav, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    def progresso(feitos: int, total: int) -> int:
        print(f"\r    {feitos / total:5.0%}", end="", flush=True)
        return 0                                   # 0 = continuar
    turnos = diarizador.process(audio, callback=progresso).sort_by_start_time()
    print("\r    100%\n")
    return [(t.start, t.end, t.speaker) for t in turnos]


def atribuir_oradores(segmentos: list[dict], turnos: list[tuple[float, float, int]]) -> list[dict]:
    """Dá a cada palavra o orador cujo turno mais se sobrepõe a ela e reparte os segmentos
    onde a voz muda. Os oradores ficam "Pessoa 1, 2…" pela ordem em que falam.
    Com uma só voz, devolve os segmentos sem rótulos."""
    sem_palavras = [{k: v for k, v in s.items() if k != "palavras"} for s in segmentos]
    if len({orador for _, _, orador in turnos}) < 2:
        print("👥 Só foi identificada uma voz.\n")
        return sem_palavras

    def orador_de(inicio: float, fim: float) -> int:
        melhor, sobreposicao = None, 0.0
        for a, b, orador in turnos:
            if min(fim, b) - max(inicio, a) > sobreposicao:
                melhor, sobreposicao = orador, min(fim, b) - max(inicio, a)
        if melhor is None:                         # fora de qualquer turno: o mais próximo
            melhor = min(turnos, key=lambda t: max(t[0] - fim, inicio - t[1], 0))[2]
        return melhor

    nomes: dict[int, str] = {}
    resultado = []
    for s, simples in zip(segmentos, sem_palavras):
        palavras = s.get("palavras") or [(s["inicio"], s["fim"], s["texto"])]
        grupos: list[list] = []                    # [orador, início, fim, texto]
        for inicio, fim, palavra in palavras:
            orador = orador_de(inicio, fim)
            if grupos and grupos[-1][0] == orador:
                grupos[-1][2], grupos[-1][3] = fim, grupos[-1][3] + palavra
            else:
                grupos.append([orador, inicio, fim, palavra])
        for orador, inicio, fim, texto in grupos:
            if not texto.strip():
                continue
            nome = nomes.setdefault(orador, f"Pessoa {len(nomes) + 1}")
            if len(grupos) == 1:
                resultado.append({**simples, "orador": nome})
            else:
                resultado.append({"inicio": inicio, "fim": fim, "texto": texto.strip(),
                                  "orador": nome})
    if len(nomes) < 2:                             # a outra voz não tinha palavras
        print("👥 Só foi identificada uma voz.\n")
        return sem_palavras
    print(f"👥 {len(nomes)} pessoas identificadas.\n")
    return resultado


# ----------------------------------------------------------------------------
# 3. Resumo (LLM local via Ollama)
# ----------------------------------------------------------------------------
# Os resumos saem em português do Brasil.
NOTA_SEM_ORADORES = "pode ter erros de reconhecimento e não identifica quem fala"
NOTA_COM_ORADORES = (
    "pode ter erros de reconhecimento; cada fala começa por quem a disse — Pessoa 1, "
    "Pessoa 2… —, uma atribuição automática que também pode ter erros. Quando estiver claro, "
    "indique quem propôs, decidiu ou ficou responsável por cada coisa, usando esses rótulos")

PROMPT_RESUMO = """Você é um assistente que escreve atas de reunião. Abaixo está {fonte} de
uma reunião de {duracao} ({nota_oradores}). Cada linha começa pelo momento da reunião em que
foi dita, no formato [mm:ss].{instrucao_partes}

Escreva, em português do Brasil, um resumo em Markdown focado nos assuntos mais importantes,
com exatamente esta estrutura:

## Resumo executivo
(2 a 4 frases: o objetivo da reunião e as principais conclusões)

## Tópicos principais
(os {n_topicos} assuntos mais importantes, do mais para o menos importante; para cada um:)

### 1. Título curto do assunto (mm:ss–mm:ss)
- 2 a 4 pontos com os fatos concretos: números, valores, datas, prazos, nomes de produtos,
  clientes ou ferramentas
- **Conclusão:** o que ficou definido, ou a posição de cada participante

## Decisões
(lista; escreva "Nenhuma decisão explícita." se não houver)

## Tarefas
(lista de tarefas combinadas na reunião, uma por linha, com o responsável e o prazo só se
tiverem sido ditos. Exemplos: "- [ ] Enviar a proposta ao cliente — Pessoa 2 — sexta-feira"
ou, sem responsável nem prazo, apenas "- [ ] Enviar a proposta ao cliente". Nunca escreva
"?", "não mencionado" ou campos vazios.)

## Questões em aberto
(dúvidas ou assuntos que os participantes deixaram explicitamente por resolver; omita esta
seção se não houver. Não invente questões, como a falta de responsável ou de prazo.)

## Outros assuntos
(assuntos menores que NÃO fazem parte dos tópicos principais, uma linha cada; omita esta
seção se não houver)

Como decidir o que é importante: o tempo dedicado ao assunto (veja os tempos [mm:ss]), se
levou a decisões ou tarefas, e o impacto no trabalho. Ignore cumprimentos, conversa paralela
e a logística da própria gravação.

Regras:
- use apenas informação que está no texto; não invente nomes, datas, números nem tarefas;
- mantenha os números e os prazos exatamente como foram ditos;
- o intervalo (mm:ss–mm:ss) de cada tópico deve vir dos tempos do texto;
- não repita o mesmo conteúdo em várias seções.

TEXTO:
\"\"\"
{texto}
\"\"\"
"""

INSTRUCAO_PARTES = """
O texto são notas tiradas de {total} partes seguidas da reunião. Antes de escolher os assuntos
mais importantes, junte os que aparecem em mais de uma parte, somando o tempo dedicado a eles."""

PROMPT_NOTAS = """Abaixo está a parte {n} de {total} da transcrição de uma reunião
({nota_oradores}). Cada linha começa pelo momento da reunião em que foi dita, no formato
[mm:ss].

Liste, em português do Brasil, os assuntos discutidos nesta parte, neste formato:

### Título curto do assunto (mm:ss–mm:ss)
- fatos concretos: números, valores, datas, prazos, nomes
- decisões tomadas
- tarefas: "tarefa — responsável — prazo" (omita o que não tiver sido mencionado)
- questões que ficaram em aberto

Inclua todos os assuntos com conteúdo, mesmo os curtos; ignore cumprimentos e a logística da
gravação. Use apenas informação que está no texto.

TRECHO:
\"\"\"
{texto}
\"\"\"
"""

NUM_CTX = 16384     # contexto do LLM em tokens; mais do que isto já não cabe nos 8 GB de VRAM
CARACTERES_POR_TOKEN = 3.3       # medido com o qwen3 em transcrições em português


def ollama(prompt: str, modelo: str, url: str, descarregar: bool = False) -> tuple[str, bool]:
    """Devolve a resposta e se o prompt foi cortado por não caber no contexto do modelo."""
    corpo = json.dumps({
        "model": modelo,
        "prompt": prompt,
        "stream": False,
        "think": False,                          # desliga o "modo raciocínio" (qwen3, etc.)
        "options": {"temperature": 0.2, "num_ctx": NUM_CTX},
        # Por defeito o Ollama mantém o modelo em VRAM 5 min; na última chamada
        # descarregamo-lo para a GPU ficar livre para o Whisper na próxima reunião.
        "keep_alive": 0 if descarregar else "5m",
    }).encode()
    pedido = urllib.request.Request(f"{url}/api/generate", data=corpo,
                                    headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(pedido, timeout=1800) as resposta:
            dados = json.loads(resposta.read())
        # O Ollama corta em silêncio o que não cabe no contexto, ficando com cerca de metade.
        # Denuncia-o ter lido muito menos tokens do que o texto tem (~3,3 caracteres por token).
        cortado = dados.get("prompt_eval_count", 0) < 0.8 * len(prompt) / CARACTERES_POR_TOKEN
        if cortado:
            print("[aviso] O texto não coube todo no contexto do modelo; "
                  "o resumo pode estar incompleto.")
        return dados["response"].strip(), cortado
    except urllib.error.HTTPError as erro:
        detalhe = erro.read().decode(errors="ignore")
        if "not found" in detalhe:
            sys.exit(f"Erro: o modelo '{modelo}' não está instalado. "
                     f"Corra: docker exec reuniao-ollama ollama pull {modelo}")
        sys.exit(f"Erro do Ollama ({erro.code}): {detalhe}")
    except urllib.error.URLError:
        sys.exit("Erro: o Ollama não está a correr. Inicie-o na pasta do projeto com: "
                 "docker compose up -d")


def dividir(texto: str, tamanho: int) -> list[str]:
    """Divide o texto em blocos de ~tamanho caracteres sem cortar frases.
    Com oradores (uma fala por linha), um bloco que começa a meio de uma fala repete
    quem está a falar, para o LLM não perder essa informação."""
    blocos, atual = [], ""
    for linha in texto.splitlines():
        # "[12:30] Pessoa 1: " — o que repetir se um bloco começar a meio desta linha
        rotulo = re.match(r"(?:\[[\d:]+\] )?(?:Pessoa \d+: )?", linha).group(0)
        for frase in re.split(r"(?<=[.?!]) ", linha):
            if len(atual) + len(frase) > tamanho and atual.strip():
                blocos.append(atual.strip())
                atual = rotulo if rotulo and not frase.startswith(rotulo) else ""
            atual += frase + " "
        atual = atual.rstrip() + "\n"
    if atual.strip():
        blocos.append(atual.strip())
    return blocos


# Campos de tarefa que o modelo preenche mesmo sem informação ("— prazo não definido", "— ?")
CAMPO_VAZIO = re.compile(r"^(\?+|-+|n/?a|.*\bn[ãa]o (mencionad|definid|especificad|informad|"
                         r"indicad|atribu[íi]d)\w*.*|.*\bsem (respons[áa]vel|prazo)\b.*|"
                         r"(respons[áa]vel|prazo):?\s*(\?+|-+)?)$", re.I)
SECOES_OPCIONAIS = ("Questões em aberto", "Outros assuntos")


def limpar_resumo(resumo: str) -> str:
    """Tira das tarefas os campos sem informação e omite as seções opcionais vazias —
    o modelo nem sempre segue essas instruções do prompt."""
    linhas = []
    for linha in resumo.splitlines():
        if re.match(r"\s*- \[ \]", linha):
            tarefa, *campos = [p.strip() for p in linha.rstrip().split(" — ")]
            linha = " — ".join([tarefa] + [c for c in campos if not CAMPO_VAZIO.match(c)])
        linhas.append(linha)
    secoes = re.split(r"(?m)^(?=## )", "\n".join(linhas))
    vazia = re.compile(r"^\s*(-\s*)?(nenhum|nenhuma|não há|não houve)\b[^\n]*\s*$", re.I)
    return "".join(s for s in secoes
                   if not (s.startswith(tuple(f"## {t}" for t in SECOES_OPCIONAIS))
                           and vazia.match(s.split("\n", 1)[1] if "\n" in s else ""))).strip()


def descrever_duracao(segundos_: float) -> tuple[str, str]:
    """("31 minutos", "3 a 6") — a duração por extenso e quantos tópicos principais pedir."""
    minutos = round(segundos_ / 60)
    if minutos < 1:
        duracao = "menos de 1 minuto"
    elif minutos < 60:
        duracao = f"{minutos} minuto{'s' if minutos > 1 else ''}"
    else:
        duracao = f"{minutos // 60} h {minutos % 60:02d} min"
    topicos = "2 a 4" if minutos <= 10 else "3 a 6" if minutos <= 45 else "4 a 8"
    return duracao, topicos


def resumir(texto: str, modelo: str, url: str, duracao_s: float) -> str:
    if not texto.strip():
        return "_A transcrição está vazia — não foi detectada fala no áudio._"
    nota = NOTA_COM_ORADORES if re.search(r"^(?:\[[\d:]+\] )?Pessoa \d+: ", texto, re.M) \
        else NOTA_SEM_ORADORES
    duracao, n_topicos = descrever_duracao(duracao_s)
    # Até ~40 min a reunião inteira cabe numa só chamada, e o modelo vê tudo para priorizar.
    blocos = dividir(texto, TAMANHO_BLOCO)
    print(f"🤖 A gerar resumo com '{modelo}'"
          + (f" (reunião longa: {len(blocos)} partes)..." if len(blocos) > 1 else "..."))
    cortes = []
    fonte, instrucao_partes = "a transcrição automática", ""
    if len(blocos) > 1:
        # Reuniões longas: primeiro tira notas de cada parte, depois junta tudo.
        notas = []
        for i, bloco in enumerate(blocos, 1):
            print(f"    parte {i}/{len(blocos)}")
            resposta, cortado = ollama(PROMPT_NOTAS.format(
                n=i, total=len(blocos), texto=bloco, nota_oradores=nota), modelo, url)
            notas.append(f"## Parte {i}\n{resposta}")
            cortes.append(cortado)
        texto = "\n\n".join(notas)
        fonte = "as notas"
        instrucao_partes = INSTRUCAO_PARTES.format(total=len(blocos))
    resumo, cortado = ollama(PROMPT_RESUMO.format(
        fonte=fonte, duracao=duracao, nota_oradores=nota, instrucao_partes=instrucao_partes,
        n_topicos=n_topicos, texto=texto), modelo, url, descarregar=True)
    resumo = limpar_resumo(resumo)
    if cortado or any(cortes):
        resumo += ("\n\n> ⚠️ A transcrição não coube toda no modelo de linguagem; "
                   "este resumo pode estar incompleto.")
    return resumo


# ----------------------------------------------------------------------------
# Fluxo principal
# ----------------------------------------------------------------------------
def nova_pasta(base: Path, titulo: str | None) -> Path:
    nome = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    if titulo:
        nome += "_" + "".join(c if c.isalnum() else "-" for c in titulo).strip("-")
    pasta = base / nome
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def processar_audio(wav: Path, pasta: Path, args) -> None:
    # Com --pessoas 1 não há vozes para distinguir.
    oradores = not args.sem_oradores and args.pessoas != 1
    segmentos = transcrever(wav, args.whisper, args.idioma, args.cpu, palavras=oradores)
    if oradores:
        segmentos = atribuir_oradores(segmentos, diarizar(wav, args.pessoas))
    texto = guardar_transcricao(segmentos, pasta)
    if args.sem_resumo:
        print(f"📁 Transcrição em: {pasta}")
    else:
        gerar_resumo(texto, segmentos[-1]["fim"] if segmentos else 0, pasta, args)


def gerar_resumo(texto: str, duracao_s: float, pasta: Path, args) -> None:
    resumo = resumir(texto, args.modelo, args.ollama_url, duracao_s)
    data = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    (pasta / "resumo.md").write_text(
        f"# Reunião — {args.titulo or pasta.name}\n\n_Gerado em {data}_\n\n{resumo}\n",
        encoding="utf-8")
    print("\n" + "=" * 70 + "\n" + resumo + "\n" + "=" * 70)
    print(f"\n📁 Ficheiros em: {pasta}")


def main():
    parser = argparse.ArgumentParser(
        description="Grava reuniões, transcreve e resume — 100% local.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__.split("Uso:")[1]) if __doc__ else None)
    def opcoes(p, padrao):
        # padrao(valor) devolve o default; nos subcomandos é SUPPRESS para não
        # sobrescrever o que foi passado antes do subcomando
        p.add_argument("--titulo", default=padrao(None),
                       help="título da reunião (usado no nome da pasta)")
        p.add_argument("--whisper", default=padrao(MODELO_WHISPER),
                       help="modelo Whisper ou pasta local")
        p.add_argument("--modelo", default=padrao(MODELO_OLLAMA),
                       help="modelo do Ollama para o resumo")
        p.add_argument("--idioma", default=padrao(IDIOMA))
        p.add_argument("--cpu", action="store_true", default=padrao(False),
                       help="forçar transcrição no CPU")
        p.add_argument("--saida", type=Path, default=padrao(PASTA_SAIDA),
                       help="pasta das reuniões")
        p.add_argument("--ollama-url", default=padrao(OLLAMA_URL))
        p.add_argument("--sem-resumo", action="store_true", default=padrao(False),
                       help="só grava e transcreve")
        p.add_argument("--pessoas", type=int, default=padrao(None),
                       help="quantas pessoas falam (por defeito é detetado sozinho)")
        p.add_argument("--sem-oradores", action="store_true", default=padrao(False),
                       help="não identificar quem fala")

    # as opções valem antes ou depois do subcomando
    opcoes(parser, lambda v: v)
    comum = argparse.ArgumentParser(add_help=False)
    opcoes(comum, lambda v: argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="comando")    # sem subcomando → interface gráfica

    sub.add_parser("gui", help="abre a interface gráfica (o mesmo que correr sem argumentos)")
    g = sub.add_parser("gravar", parents=[comum], help="grava uma reunião nova")
    g.add_argument("--dispositivo", help="número ou nome do microfone (ver 'microfones')")
    g.add_argument("--meet", action="store_true",
                   help="reunião online (Meet, Teams, Zoom…): grava também o som do sistema")
    p = sub.add_parser("processar", parents=[comum], help="processa um ficheiro de áudio existente")
    p.add_argument("audio", type=Path)
    r = sub.add_parser("resumir", parents=[comum],
                       help="gera o resumo a partir de uma transcrição em texto")
    r.add_argument("transcricao", type=Path)
    sub.add_parser("microfones", help="lista os dispositivos de áudio")
    args = parser.parse_args()
    if args.pessoas is not None and args.pessoas < 1:
        parser.error("--pessoas tem de ser 1 ou mais")

    if args.comando in (None, "gui"):
        # A interface usa o GTK do sistema (python3-gi), que não está no .venv.
        os.execv("/usr/bin/python3", ["python3", str(Path(__file__).with_name("interface.py"))])
    elif args.comando == "microfones":
        listar_dispositivos()
    elif args.comando == "gravar":
        pasta = nova_pasta(args.saida, args.titulo)
        disp = int(args.dispositivo) if args.dispositivo and args.dispositivo.isdigit() \
            else args.dispositivo
        # --dispositivo também aceita o nome de uma fonte do PulseAudio (é o que a interface usa)
        fonte = disp if isinstance(disp, str) and disp in fontes_pulse() else None
        if args.meet:
            # grava do sink misturado através do PulseAudio
            with capturar_sistema(fonte):
                wav = gravar(pasta / "audio.wav", "pulse")
        elif fonte:
            os.environ["PULSE_SOURCE"] = fonte
            wav = gravar(pasta / "audio.wav", "pulse")
        else:
            wav = gravar(pasta / "audio.wav", disp)
        processar_audio(wav, pasta, args)
    elif args.comando == "processar":
        if not args.audio.exists():
            sys.exit(f"Erro: ficheiro não encontrado: {args.audio}")
        pasta = nova_pasta(args.saida, args.titulo or args.audio.stem)
        print(f"🎧 A converter {args.audio.name} para {pasta / 'audio.wav'}")
        wav = para_wav(args.audio, pasta / "audio.wav")
        processar_audio(wav, pasta, args)
    elif args.comando == "resumir":
        # "[mm:ss] Pessoa N: texto" ou "[mm:ss] texto" → segmentos, para juntar as falas
        segmentos = []
        for linha in args.transcricao.read_text(encoding="utf-8").splitlines():
            partes = re.match(r"(?:\[([\d:]+)\] )?(?:(Pessoa \d+): )?(.*)", linha)
            if partes[3].strip():
                inicio = segundos(partes[1]) if partes[1] else \
                    (segmentos[-1]["inicio"] if segmentos else 0.0)
                segmentos.append({"inicio": inicio, "texto": partes[3],
                                  **({"orador": partes[2]} if partes[2] else {})})
        duracao = segmentos[-1]["inicio"] if segmentos else 0
        gerar_resumo(texto_para_resumo(segmentos), duracao, args.transcricao.parent, args)


if __name__ == "__main__":
    main()
