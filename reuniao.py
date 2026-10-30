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
import atexit
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
MODELO_OLLAMA = "qwen3.5:4b"           # alternativas: qwen3:8b, gemma4:e4b (mais pesados)
OLLAMA_URL = "http://localhost:11434"
TAXA_AMOSTRAGEM = 16000                # 16 kHz mono é o que o Whisper usa
TAMANHO_BLOCO = 12000                  # caracteres por bloco de extração de notas (~10 min)
LIMITE_UMA_CHAMADA = 500               # só transcrições minúsculas saem numa só chamada
LIMITE_NOTAS = 40000                   # notas maiores são condensadas antes do resumo

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

    total = len(audio) / TAXA_AMOSTRAGEM

    def correr(modelo) -> list[dict]:
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
        return resultado

    modelo = carregar_modelo(nome_modelo, forcar_cpu)
    try:
        resultado = correr(modelo)
    except RuntimeError as erro:
        # Sem as bibliotecas CUDA (fora do atalho `reuniao`), a GPU só falha ao transcrever.
        if forcar_cpu or modelo.model.device == "cpu":
            raise
        print(f"\n[aviso] GPU indisponível ({erro}). A usar CPU.")
        modelo = carregar_modelo(nome_modelo, True)
        resultado = correr(modelo)
    del modelo                                 # liberta a VRAM antes do Ollama carregar o LLM
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


def usar_gpu_oradores(forcar_cpu: bool) -> bool:
    """A GPU só compensa com a versão CUDA do sherpa-onnx e as bibliotecas NVIDIA carregáveis
    (o atalho `reuniao` põe-nas no LD_LIBRARY_PATH). Se faltar alguma, o sherpa cai sozinho
    para o CPU com 1 fio e fica mais lento do que o CPU normal — por isso decide-se aqui."""
    if forcar_cpu or not shutil.which("nvidia-smi"):
        return False
    import ctypes
    import sherpa_onnx
    if "cuda" not in sherpa_onnx.__version__:
        return False
    try:
        for biblioteca in ("libcublasLt.so.12", "libcudnn.so.9"):
            ctypes.CDLL(biblioteca)
    except OSError:
        return False
    return True


def diarizar_local(wav: Path, pessoas: int | None, forcar_cpu: bool) -> None:
    """Corre no processo filho (subcomando interno `_oradores`) e escreve no stdout:
    "MODO GPU|CPU", "PROGRESSO N" (0–100) e, no fim, "TURNOS [[início, fim, orador], …]".

    É um processo à parte por duas razões: o sherpa-onnx em CUDA quebra se for criado depois
    do Whisper no mesmo processo, e assim a identificação corre em paralelo com a transcrição.
    """
    import sherpa_onnx
    import soundfile as sf

    segmentacao, vozes = modelos_oradores()
    gpu = usar_gpu_oradores(forcar_cpu)
    print(f"MODO {'GPU' if gpu else 'CPU'}", flush=True)
    provider, fios = ("cuda", 1) if gpu else ("cpu", max(1, (os.cpu_count() or 2) // 2))
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(segmentacao)),
            num_threads=fios, provider=provider),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(vozes), num_threads=fios, provider=provider),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=pessoas or -1,
                                                    threshold=LIMIAR_ORADORES),
        min_duration_on=0.3, min_duration_off=0.5)
    diarizador = sherpa_onnx.OfflineSpeakerDiarization(config)

    audio, _ = sf.read(wav, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    ultimo = -1

    def progresso(feitos: int, total: int) -> int:
        nonlocal ultimo
        pct = 100 * feitos // total
        if pct != ultimo:                          # uma linha por ponto percentual, no máximo
            ultimo = pct
            print(f"PROGRESSO {pct}", flush=True)
        return 0                                   # 0 = continuar
    turnos = diarizador.process(audio, callback=progresso).sort_by_start_time()
    print("TURNOS " + json.dumps([(t.start, t.end, t.speaker) for t in turnos]), flush=True)


class Oradores:
    """Identificação de quem fala num processo à parte, lançada antes do Whisper.
    `terminar()` espera por ela, mostra o progresso que falta e devolve os turnos."""

    def __init__(self, wav: Path, pessoas: int | None, forcar_cpu: bool):
        modelos_oradores()           # o download (e o seu erro) acontecem no processo principal
        comando = [sys.executable, str(Path(__file__).resolve()), "_oradores", str(wav)]
        if pessoas:
            comando += ["--pessoas", str(pessoas)]
        if forcar_cpu:
            comando.append("--cpu")
        self.proc = subprocess.Popen(comando, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True)
        # Se o processo principal morrer (Ctrl+C, fechar a janela), o filho não fica órfão.
        atexit.register(lambda: self.proc.poll() is None and self.proc.kill())
        modo = self.proc.stdout.readline().strip().removeprefix("MODO ") or "CPU"
        print(f"👥 A identificar quem fala ({modo}, em paralelo com a transcrição)...")

    def terminar(self) -> list[tuple[float, float, int]]:
        if self.proc.poll() is None:
            print("👥 A terminar a identificação de quem fala...")
        turnos = None
        for linha in self.proc.stdout:
            if linha.startswith("PROGRESSO "):
                print(f"\r    {int(linha.split()[1]):3d}%", end="", flush=True)
            elif linha.startswith("TURNOS "):
                turnos = [tuple(t) for t in json.loads(linha[7:])]
        erro = self.proc.stderr.read()
        self.proc.wait()
        if turnos is None or self.proc.returncode != 0:
            detalhe = (erro.strip().splitlines() or ["sem detalhes"])[-1]
            print(f"\n[aviso] A identificação de quem fala falhou ({detalhe}). "
                  "A transcrição fica sem oradores.\n")
            return []
        print("\r    100%\n")
        return turnos


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

PROMPT_NOTAS = """Abaixo está a parte {n} de {total} da transcrição de uma reunião ({nota_oradores}).
Cada linha começa pelo momento da reunião em que foi dita, no formato [mm:ss].

Sua tarefa é EXTRAIR, não resumir. Divida o trecho em 3 a 6 assuntos (um assunto dura em
geral vários minutos; não crie um assunto por frase) e liste-os, em português do Brasil e em
ordem cronológica, neste formato:

### <nome do assunto, de 3 a 8 palavras> (mm:ss–mm:ss)
- Fatos: até 6 pontos, um por linha, com no máximo 20 palavras cada: toda ferramenta,
  biblioteca, versão, sistema, número, valor, data e nome citados, com o papel de cada um
  (por exemplo: "Lombok: anotações que geram getters, setters e construtores no build")
- Divergências: só se os participantes defenderam coisas diferentes, e quem defendeu o quê
- Decisões: só escolhas feitas na reunião (entre alternativas, ou "vou manter X", "fica
  decidido Y"), e quem as fez; descrever como algo já funciona ou já foi feito não é decisão
- Tarefas: "ação curta, com o verbo no infinitivo — quem — prazo", sem citar a frase dita e
  sem brincadeiras; o prazo é copiado tal como foi dito (por exemplo "hoje à tarde")
- Em aberto: só perguntas feitas na reunião que ninguém respondeu

Omita as linhas que ficariam vazias (não escreva "nenhuma"). Inclua também as explicações
técnicas; ignore apenas cumprimentos, conversa paralela e problemas de áudio; não avalie a
importância; não copie frases soltas da transcrição; não acrescente nada que não esteja no
trecho.

TRECHO:
\"\"\"
{texto}
\"\"\"
"""

PROMPT_RESUMO = """Você é um assistente que escreve atas de reunião. A reunião foi em {data} e
durou {duracao} ({nota_oradores}). Abaixo estão {fonte}; os tempos [mm:ss] e (mm:ss–mm:ss)
indicam o momento da reunião.{instrucao_partes}

Escreva, em português do Brasil, um resumo em Markdown com exatamente esta estrutura:

## Resumo executivo
(3 a 5 frases: o objetivo da reunião, o resultado principal e o que vem a seguir)

## Tópicos principais
(os {n_topicos} assuntos mais importantes, do mais para o menos importante; para cada um:)

### 1. Título curto do assunto (mm:ss–mm:ss)
- um fato concreto por linha, até 8 linhas, sem o prefixo "Fatos:": nomes de ferramentas,
  bibliotecas, versões, sistemas, números, valores, datas e prazos, exatamente como estão no
  texto; cada linha começa pelo assunto (a ferramenta, a pessoa, o sistema), nunca por "Foi"
- **Conclusão:** o que ficou definido, ou a posição de cada participante

## Decisões
(no máximo 6, só as explícitas, com quem decidiu; descrever como algo funciona não é
decisão. Escreva "Nenhuma decisão explícita." se não houver)

## Tarefas
(no máximo 8, uma por linha: "- [ ] ação curta, com o verbo no infinitivo — responsável — prazo". Copie o
responsável e o prazo exatamente como estão no texto, sem os trocar nem completar; só entram
se tiverem sido ditos. Junte as tarefas repetidas. Nunca acrescente uma hora, um dia da semana
ou uma data que não esteja no texto; nunca escreva "?", "não mencionado" ou campos vazios.)

## Questões em aberto
(só dúvidas que ficaram sem resposta; uma dúvida resolvida na reunião não entra. Omita a
seção se não houver.)

## Outros assuntos
(os assuntos do texto que não entraram em nenhum tópico principal, pelo título, uma linha
cada e no máximo 5; nunca repita aqui o que já está nos tópicos. Omita a seção se não houver.)

Como decidir a importância: o tempo dedicado ao assunto, se levou a decisões ou tarefas, e o
impacto no trabalho. Cumprimentos, conversa paralela e a logística da gravação não contam.

Regras:
- cada tópico principal é um tema amplo: junte nele todos os assuntos do texto que lhe
  pertencem, mesmo que apareçam em momentos diferentes da reunião;
- todo assunto com conteúdo tem de aparecer, num tópico principal ou em Outros assuntos;
- o intervalo (mm:ss–mm:ss) de cada tópico vem dos tempos do texto, e os intervalos de
  tópicos diferentes não se sobrepõem;
- escreva frases diretas; não use "foi mencionado que" nem "foi discutido que", e não
  generalize ("seguindo os padrões" não serve: diga quais);
- não invente nomes, datas, números nem tarefas; não repita o mesmo conteúdo em várias seções;
- responda só com o Markdown, sem cercas de código.

TEXTO:
\"\"\"
{texto}
\"\"\"
"""

INSTRUCAO_PARTES = """
As notas foram extraídas de {total} partes seguidas da reunião. Os tópicos principais já foram
escolhidos e ordenados; escreva-os exatamente com estes títulos, nesta ordem e com estes
intervalos, juntando em cada um tudo o que as notas têm sobre ele:
{estrutura}
Em "Decisões" entram só as linhas "Decisões" das notas; em "Questões em aberto" só as linhas
"Em aberto"; em "Tarefas" só as linhas "Tarefas". Uma conclusão de tópico não é uma decisão."""

PROMPT_ESTRUTURA = """Abaixo estão notas detalhadas, em ordem cronológica, de uma reunião de
{duracao}, extraídas de {total} partes; os intervalos (mm:ss–mm:ss) indicam o momento da
reunião.

Agrupe os assuntos das notas em {n_topicos} temas (nunca menos do que isso) e ordene-os do
mais importante para o menos importante. Um tema junta assuntos sobre a mesma questão, mesmo
de partes diferentes, mas tem de ser coeso: não junte assuntos diferentes só porque falam do
mesmo sistema. Um tema é importante pelo tempo que ocupou, por ter levado a decisões ou
tarefas e pelo impacto no trabalho; cumprimentos, piadas e logística da gravação não contam
e não formam temas. Todo assunto com conteúdo tem de pertencer a um tema ou ficar em "Outros".

Responda só com esta lista, em português do Brasil, sem comentários:
1. Título do tema (mm:ss–mm:ss, mm:ss–mm:ss se tiver mais de um intervalo) — assuntos das notas que ele junta
2. …
Outros: títulos dos assuntos que ficaram de fora, separados por ponto e vírgula (ou "nenhum")

NOTAS:
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
SECOES_OPCIONAIS = ("Tarefas", "Questões em aberto", "Outros assuntos")   # somem se vazias
DIAS_SEMANA = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


def prazo_inventado(campo: str, conhecido: str, data: dt.date) -> bool:
    """Um dia da semana ou uma data dd/mm que não está no texto da reunião (nem é a data
    da reunião ou a semana seguinte) foi inventado pelo modelo."""
    texto = conhecido.lower()
    for dia in DIAS_SEMANA:
        if dia in campo.lower() and dia not in texto:
            return True
    for hora in re.findall(r"\b(\d{1,2})\s*(?:h\b|:\d{2}|horas)", campo.lower()):
        if not re.search(rf"\b{hora}\s*(?:h\b|:\d{{2}}|horas)|\bdas {hora}\b", texto):
            return True
    if re.search(r"imediat|urgente|o quanto antes", campo, re.I) and not \
            re.search(r"imediat|urgente|o quanto antes", texto):
        return True
    for d, m in re.findall(r"\b(\d{1,2})/(\d{1,2})\b", campo):
        if f"{d}/{m}" in texto or f"{int(d):02d}/{int(m):02d}" in texto:
            continue
        try:
            quando = dt.date(data.year, int(m), int(d))
        except ValueError:
            return True
        if not 0 <= (quando - data).days <= 7:
            return True
    return False


VERBOS_GENERICOS = {"criar", "adicionar", "fazer", "ajustar", "implementar", "configurar",
                    "colocar", "deixar", "definir", "realizar", "finalizar", "preparar"}


def limpar_resumo(resumo: str, conhecido: str = "", data: dt.date | None = None,
                  omitir: tuple[str, ...] = ()) -> str:
    """Arruma o que o modelo nem sempre faz apesar do prompt: tira cercas de código à volta
    da resposta, tarefas repetidas, campos de tarefa sem informação ou com prazos inventados,
    as seções opcionais vazias e as seções em `omitir` (sem base nas notas)."""
    texto = resumo.strip()
    if texto.startswith("```"):
        texto = texto.split("\n", 1)[1] if "\n" in texto else ""
        if texto.rstrip().endswith("```"):
            texto = texto.rstrip()[:-3]
    # "Foi mencionado que o Lombok…" → "O Lombok…": o modelo insiste nesta muleta
    texto = re.sub(r"(?im)^(\s*(?:-\s*|\d+\.\s*)?(?:\*\*)?)(?:foi|ficou) (?:mencionad[oa]|"
                   r"discutid[oa]|decidid[oa]|sugerid[oa]|destacad[oa]|confirmad[oa]|"
                   r"levantad[oa]|comentad[oa]|definid[oa]|identificad[oa]|acordad[oa])"
                   r"(?: que)?\s+(\w)",
                   lambda m: m.group(1) + m.group(2).upper(), texto)
    linhas, tarefas_vistas, seccao = [], [], ""
    for linha in texto.splitlines():
        if linha.startswith("## "):
            seccao = linha[3:].strip()
        if seccao == "Tarefas" and re.match(r"\s*[-*]\s+", linha) \
                and not re.match(r"\s*[-*]\s*(\[ \]\s*)?(nenhum|não )", linha, re.I):
            # nem sempre vem a caixa "- [ ]" nem o travessão; "Pessoa 1 – ação" passa a
            # "ação — Pessoa 1"
            linha = re.sub(r"^\s*[-*]\s+(?!\[)", "- [ ] ", linha)
            linha = re.sub(r"\s+[–-]\s+", " — ", linha)
            linha = re.sub(r"^- \[ \] (Pessoa \d+)\s*—\s*(.+?)\s*$", r"- [ ] \2 — \1", linha)
        elif (seccao == "Tópicos principais" and re.match(r"\s*- (?!\*\*)", linha)
              and len(linha) > 180 and "; " in linha):
            # um fato por linha: às vezes vem tudo numa só, separado por ";"
            linhas.extend("- " + p.strip().rstrip(".")
                          for p in linha.strip()[2:].split("; ") if p.strip())
            continue
        linha = re.sub(r"^(\s*- )<(.+)>\s*$", r"\1\2", linha)     # "<título>" literal
        if re.match(r"\s*- \[ \]", linha):
            tarefa, *campos = [p.strip() for p in linha.rstrip().split(" — ")]
            # tarefas repetidas com outras palavras ("Criar Docker Compose" depois de
            # "Adicionar o Docker Compose e o Dockerfile"): fica a primeira
            palavras = {p for p in re.findall(r"\w{4,}", tarefa.lower())} - VERBOS_GENERICOS
            if palavras and any(palavras <= vista for vista in tarefas_vistas):
                continue
            tarefas_vistas.append(palavras)
            campos = [c for c in campos if not CAMPO_VAZIO.match(c)
                      and not (data and prazo_inventado(c, conhecido, data))]
            linha = " — ".join([tarefa] + campos)
        linhas.append(linha)
    secoes = re.split(r"(?m)^(?=## )", "\n".join(linhas))
    # "Nenhuma.", "Nenhuma tarefa foi mencionada", "A seção está omitida, pois…"
    vazia = re.compile(r"^\s*(-\s*)?(nenhum|nenhuma|não há|não houve|não foi|não foram|"
                       r"a seção|esta seção|seção omitida|sem tarefas|sem questões)\b[^\n]*\s*$",
                       re.I)
    return "".join(s for s in secoes
                   if not s.startswith(tuple(f"## {t}" for t in omitir))
                   and not (s.startswith(tuple(f"## {t}" for t in SECOES_OPCIONAIS))
                            and vazia.match(s.split("\n", 1)[1] if "\n" in s else ""))).strip()


def descrever_duracao(segundos_: float) -> tuple[str, str]:
    """("31 minutos", "4 a 7") — a duração por extenso e quantos tópicos principais pedir."""
    minutos = round(segundos_ / 60)
    if minutos < 1:
        duracao = "menos de 1 minuto"
    elif minutos < 60:
        duracao = f"{minutos} minuto{'s' if minutos > 1 else ''}"
    else:
        duracao = f"{minutos // 60} h {minutos % 60:02d} min"
    topicos = "2 a 4" if minutos <= 10 else "3 a 5" if minutos <= 20 else "4 a 7"
    return duracao, topicos


def data_de(caminho: Path) -> dt.date:
    """Data da reunião: AAAA-MM-DD no nome do ficheiro/pasta ou, na falta, a data do ficheiro."""
    # A pasta de uma reunião chama-se "<data do processamento>_<hora>_<título>", e o título
    # pode trazer a data original do ficheiro; por isso vale a última data encontrada.
    datas = re.findall(r"(20\d\d)-(\d\d)-(\d\d)", caminho.name)
    if datas:
        with contextlib.suppress(ValueError):
            return dt.date(*map(int, datas[-1]))
    if caminho.exists():
        return dt.date.fromtimestamp(caminho.stat().st_mtime)
    return dt.date.today()


def descrever_data(data: dt.date) -> str:
    dias = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira",
            "sábado", "domingo")
    return f"{data:%d/%m/%Y} ({dias[data.weekday()]})"


PROMPT_SINTESE = """Você é um assistente que escreve atas de reunião. A reunião foi em {data} e
durou {duracao} ({nota_oradores}). Abaixo estão as notas da reunião, já organizadas por tema
e por ordem de importância; os tempos (mm:ss–mm:ss) indicam o momento da reunião.

Escreva, em português do Brasil, um resumo em Markdown com exatamente esta estrutura:

## Resumo executivo
(3 a 5 frases: o objetivo da reunião, o resultado principal e o que vem a seguir)

## Tópicos principais
(um tópico por tema, com os títulos, a ordem e os intervalos dados; para cada um:)

### 1. Título do tema (mm:ss–mm:ss)
- um fato concreto por linha, até 8 linhas, só com o que está nas notas desse tema: nomes de
  ferramentas, bibliotecas, versões, sistemas, números, valores, datas e prazos, exatamente
  como estão nas notas; cada linha começa pelo assunto (a ferramenta, a pessoa, o sistema),
  nunca por "Foi"
- **Conclusão:** o que ficou definido, ou a posição de cada participante

## Decisões
(reescreva a lista "Decisões registradas" dada abaixo, sem acrescentar nada e juntando as
repetidas; escreva "Nenhuma decisão explícita." se a lista estiver vazia)

## Tarefas
(reescreva a lista "Tarefas registradas", uma por linha: "- [ ] ação curta, com o verbo no
infinitivo — responsável — prazo", copiando o responsável e o prazo exatamente como estão e
juntando as repetidas; nunca acrescente uma hora, um dia da semana ou uma data)

## Questões em aberto
(reescreva a lista "Perguntas sem resposta"; omita a seção se a lista estiver vazia)

## Outros assuntos
(os títulos da lista "Fora dos temas", uma linha cada; omita a seção se a lista estiver vazia)

Regras: não repita um fato em dois tópicos; escreva frases diretas, sem "foi mencionado que"
nem "foi discutido que"; não invente nomes, datas, números nem tarefas; responda só com o
Markdown, sem cercas de código.

NOTAS:
\"\"\"
{texto}
\"\"\"
"""


def seccoes_das_notas(notas: list[str]) -> list[dict]:
    """Parte as notas pelos cabeçalhos "### Assunto (mm:ss–mm:ss)" em
    {titulo, inicio, fim, texto, decisoes, tarefas, abertas}."""
    seccoes = []
    for nota in notas:
        for bloco in re.split(r"(?m)^(?=### )", nota):
            if not bloco.startswith("### "):
                continue
            cabecalho, _, corpo = bloco.partition("\n")
            tempos = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)\s*[–-]\s*(\d{1,2}:\d{2}(?::\d{2})?)",
                               cabecalho)
            titulo = re.sub(r"[\[\(]?\d{1,2}:\d{2}(?::\d{2})?\s*[–-]\s*\d{1,2}:\d{2}(?::\d{2})?[\]\)]?",
                            "", cabecalho[4:]).strip(" -–:")
            campos: dict[str, list[str]] = {"decisoes": [], "tarefas": [], "abertas": []}
            atual = None
            for linha in corpo.splitlines():
                campo = re.match(r"\s*-\s*(Fatos|Divergências|Decisões|Tarefas|Em aberto):\s*(.*)",
                                 linha, re.I)
                if campo:
                    atual = {"decisões": "decisoes", "tarefas": "tarefas",
                             "em aberto": "abertas"}.get(campo[1].lower())
                    if atual and campo[2].strip():
                        campos[atual].append(campo[2].strip())
                elif atual and re.match(r"\s+-\s*\S", linha):
                    campos[atual].append(linha.strip()[1:].strip())
            # "Não há dúvidas", "nenhuma" — o modelo nem sempre omite as linhas vazias
            for campo in campos:
                campos[campo] = [i for i in campos[campo]
                                 if not re.match(r"(n[ãa]o h[áa]|nenhum)", i, re.I)]
            seccoes.append({"titulo": titulo or "Assunto",
                            "inicio": segundos(tempos[1]) if tempos else None,
                            "fim": segundos(tempos[2]) if tempos else None,
                            "texto": bloco.strip(), **campos})
    return seccoes


def organizar_notas(estrutura: str, seccoes: list[dict]) -> tuple[list[tuple[str, list[dict]]],
                                                                   list[dict]]:
    """Lê a estrutura ("1. Tema (mm:ss–mm:ss, …) — …") e distribui cada secção das notas pelo
    primeiro tema cujo intervalo a contém. Devolve [(título do tema, secções)] e as secções
    que ficaram fora."""
    temas = []
    for linha in estrutura.splitlines():
        tema = re.match(r"\s*\d+[.)]\s*(.+?)\s*\(([^)]*\d[^)]*)\)", linha)
        if not tema:
            continue
        intervalos = [(segundos(a), segundos(b)) for a, b in
                      re.findall(r"(\d{1,2}:\d{2}(?::\d{2})?)\s*[–-]\s*(\d{1,2}:\d{2}(?::\d{2})?)",
                                 tema[2])]
        temas.append((tema[1].strip(), intervalos, []))
    fora = []
    for s in seccoes:
        meio = (s["inicio"] + s["fim"]) / 2 if s["inicio"] is not None else None
        destino = next((t for t in temas if meio is not None
                        and any(a <= meio <= b for a, b in t[1])), None)
        (destino[2] if destino else fora).append(s)
    return [(titulo, secs) for titulo, _, secs in temas if secs], fora


def sem_repetidos(itens: list[str], maximo: int) -> list[str]:
    """Tira itens cujas palavras de conteúdo já estão todas num item anterior, e limita."""
    vistos: list[set[str]] = []
    resultado = []
    for item in itens:
        palavras = set(re.findall(r"\w{4,}", item.lower())) - VERBOS_GENERICOS
        if palavras and any(palavras <= v for v in vistos):
            continue
        vistos.append(palavras)
        resultado.append(item)
    return resultado[:maximo]


def texto_organizado(temas: list[tuple[str, list[dict]]], fora: list[dict],
                     seccoes: list[dict]) -> str:
    def intervalo(secs):
        tempos = [(s["inicio"], s["fim"]) for s in secs if s["inicio"] is not None]
        return (f"{formatar_tempo(min(a for a, _ in tempos))}–"
                f"{formatar_tempo(max(b for _, b in tempos))}") if tempos else ""

    vistas: set[str] = set()

    def sem_factos_repetidos(texto: str) -> str:
        # o mesmo fato extraído em duas secções só fica na primeira
        linhas = []
        for linha in texto.splitlines():
            chave = re.sub(r"\W+", " ", linha.lower()).strip()
            if re.match(r"\s+-\s", linha) and chave in vistas:
                continue
            vistas.add(chave)
            linhas.append(linha)
        return "\n".join(linhas)

    partes = [f"## Tema {i}: {titulo} ({intervalo(secs)})\n\n"
              + "\n\n".join(sem_factos_repetidos(s["texto"]) for s in secs)
              for i, (titulo, secs) in enumerate(temas, 1)]
    partes.append("## Fora dos temas\n" + ("\n".join(f"- {s['titulo']}" for s in fora) or "- (nenhum)"))
    for rotulo, campo, maximo in (("Decisões registradas", "decisoes", 6),
                                  ("Tarefas registradas", "tarefas", 8),
                                  ("Perguntas sem resposta", "abertas", 5)):
        itens = sem_repetidos([item for s in seccoes for item in s[campo]], maximo)
        partes.append(f"## {rotulo}\n" + ("\n".join(f"- {i}" for i in itens) or "- (nenhuma)"))
    return "\n\n".join(partes)


def resumir(texto: str, modelo: str, url: str, duracao_s: float,
            data: dt.date | None = None) -> str:
    """Até ~5 min de fala, uma só chamada sobre a transcrição. Acima disso, em três passos:
    1) extrair notas detalhadas de cada bloco de ~10 min (onde o modelo não perde pormenores);
    2) escolher e ordenar os temas a partir das notas (um passo à parte, senão o modelo segue
       a ordem cronológica e deixa o fim da reunião de fora);
    3) escrever o resumo a partir das notas já agrupadas por tema."""
    if not texto.strip():
        return "_A transcrição está vazia — não foi detectada fala no áudio._"
    data = data or dt.date.today()
    nota = NOTA_COM_ORADORES if re.search(r"^(?:\[[\d:]+\] )?Pessoa \d+: ", texto, re.M) \
        else NOTA_SEM_ORADORES
    duracao, n_topicos = descrever_duracao(duracao_s)
    cortes = []

    def chamar(prompt: str, **extra) -> str:
        resposta, cortado = ollama(prompt, modelo, url, **extra)
        cortes.append(cortado)
        return resposta

    comuns = dict(duracao=duracao, data=descrever_data(data), nota_oradores=nota)
    omitir: list[str] = []
    if len(texto) <= LIMITE_UMA_CHAMADA:
        print(f"🤖 A gerar resumo com '{modelo}'...")
        resumo = chamar(PROMPT_RESUMO.format(fonte="a transcrição automática",
                                             instrucao_partes="", n_topicos=n_topicos,
                                             texto=texto, **comuns), descarregar=True)
    else:
        blocos = dividir(texto, TAMANHO_BLOCO)
        print(f"🤖 A gerar resumo com '{modelo}' ({len(blocos)} partes)...")
        notas = []
        for i, bloco in enumerate(blocos, 1):
            print(f"    parte {i}/{len(blocos)}")
            notas.append(chamar(PROMPT_NOTAS.format(n=i, total=len(blocos), texto=bloco,
                                                    nota_oradores=nota)))
        # Reuniões de várias horas: as notas podem não caber; condensam-se em metades.
        for _ in range(2):
            if sum(map(len, notas)) <= LIMITE_NOTAS or len(notas) < 2:
                break
            print(f"    a condensar {len(notas)} partes de notas")
            metade = (len(notas) + 1) // 2
            notas = [chamar(PROMPT_NOTAS.format(n=i, total=2, texto="\n\n".join(grupo),
                                                nota_oradores=nota))
                     for i, grupo in enumerate((notas[:metade], notas[metade:]), 1) if grupo]
        # "Em aberto: não há dúvidas" é o modelo a não obedecer ao "omita a linha".
        notas = [re.sub(r"(?im)^\s*-\s*(em aberto|decisões|tarefas):.*\b(n[ãa]o h[áa]|nenhum)\b.*\n?",
                        "", n) for n in notas]
        texto_notas = "\n\n".join(f"## Parte {i}\n{n}" for i, n in enumerate(notas, 1))
        print("    a organizar os temas")
        estrutura = chamar(PROMPT_ESTRUTURA.format(duracao=duracao, total=len(blocos),
                                                   n_topicos=n_topicos, texto=texto_notas))
        seccoes = seccoes_das_notas(notas)
        temas, fora = organizar_notas(estrutura, seccoes)
        if temas:
            texto_notas = texto_organizado(temas, fora, seccoes)
        if not any(s["abertas"] for s in seccoes):
            omitir.append("Questões em aberto")
        if temas and not fora:
            omitir.append("Outros assuntos")
        print("    a escrever o resumo")
        resumo = chamar(PROMPT_SINTESE.format(texto=texto_notas, **comuns), descarregar=True)
    resumo = limpar_resumo(resumo, conhecido=texto, data=data, omitir=tuple(omitir))
    if any(cortes):
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
    if oradores:
        identificacao = Oradores(wav, args.pessoas, args.cpu)     # arranca já, em paralelo
    segmentos = transcrever(wav, args.whisper, args.idioma, args.cpu, palavras=oradores)
    if oradores:
        turnos = identificacao.terminar()
        segmentos = atribuir_oradores(segmentos, turnos) if turnos else [
            {k: v for k, v in s.items() if k != "palavras"} for s in segmentos]
    texto = guardar_transcricao(segmentos, pasta)
    if args.sem_resumo:
        print(f"📁 Transcrição em: {pasta}")
    else:
        gerar_resumo(texto, segmentos[-1]["fim"] if segmentos else 0, pasta, args)


def gerar_resumo(texto: str, duracao_s: float, pasta: Path, args) -> None:
    data = getattr(args, "data_reuniao", None) or data_de(pasta)
    resumo = resumir(texto, args.modelo, args.ollama_url, duracao_s, data)
    agora = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    (pasta / "resumo.md").write_text(
        f"# Reunião — {args.titulo or pasta.name}\n\n"
        f"_Reunião de {descrever_data(data)}; resumo gerado em {agora}_\n\n{resumo}\n",
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
    # sem subcomando → interface gráfica; o metavar esconde o subcomando interno `_oradores`
    sub = parser.add_subparsers(dest="comando", metavar="comando")

    sub.add_parser("gui", help="abre a interface gráfica (o mesmo que correr sem argumentos)")
    # interno: a identificação de quem fala corre neste subcomando, num processo filho.
    # Só é registado quando pedido, para não aparecer na ajuda.
    if sys.argv[1:2] == ["_oradores"]:
        sub.add_parser("_oradores", parents=[comum]).add_argument("audio", type=Path)
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

    # Um `kill` (ou fechar a janela/terminal) termina como o Ctrl+C: a gravação fecha o WAV e
    # limpa o áudio do sistema, e o processo de identificação de oradores é terminado (atexit).
    def terminar(sinal, frame):
        raise KeyboardInterrupt
    for sinal in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sinal, terminar)

    if args.comando in (None, "gui"):
        # A interface usa o GTK do sistema (python3-gi), que não está no .venv.
        os.execv("/usr/bin/python3", ["python3", str(Path(__file__).with_name("interface.py"))])
    elif args.comando == "_oradores":
        diarizar_local(args.audio, args.pessoas, args.cpu)
    elif args.comando == "microfones":
        listar_dispositivos()
    elif args.comando == "gravar":
        args.data_reuniao = dt.date.today()
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
        args.data_reuniao = data_de(args.audio)      # do nome (AAAA-MM-DD) ou da data do ficheiro
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
        args.data_reuniao = data_de(args.transcricao.parent)
        gerar_resumo(texto_para_resumo(segmentos), duracao, args.transcricao.parent, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:                  # Ctrl+C ou kill fora da gravação
        sys.exit("\nInterrompido.")
