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
import shutil
import signal
import subprocess
import sys
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
TAMANHO_BLOCO = 12000                  # caracteres por bloco nas reuniões longas


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
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(origem),
                    "-ar", str(TAXA_AMOSTRAGEM), "-ac", "1", str(destino)], check=True)
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


def transcrever(wav: Path, nome_modelo: str, idioma: str, forcar_cpu: bool) -> list[dict]:
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
    )
    resultado = []
    for s in segmentos:
        resultado.append({"inicio": s.start, "fim": s.end, "texto": s.text.strip()})
        print(f"\r    {s.end / total:5.0%}", end="", flush=True)
    print("\r    100%\n")
    del segmentos, modelo                      # liberta a VRAM antes do Ollama carregar o LLM
    return resultado


def formatar_tempo(segundos: float) -> str:
    h, resto = divmod(int(segundos), 3600)
    m, s = divmod(resto, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def guardar_transcricao(segmentos: list[dict], pasta: Path) -> str:
    texto_corrido = " ".join(s["texto"] for s in segmentos)
    com_tempos = "\n".join(f"[{formatar_tempo(s['inicio'])}] {s['texto']}" for s in segmentos)
    (pasta / "transcricao.txt").write_text(com_tempos + "\n", encoding="utf-8")
    (pasta / "transcricao.json").write_text(
        json.dumps(segmentos, ensure_ascii=False, indent=2), encoding="utf-8")
    return texto_corrido


# ----------------------------------------------------------------------------
# 3. Resumo (LLM local via Ollama)
# ----------------------------------------------------------------------------
PROMPT_RESUMO = """És um assistente que escreve atas de reuniões. Abaixo está a transcrição
automática de uma reunião presencial (pode ter erros de reconhecimento e não identifica
quem fala). Escreve, em português, um resumo em Markdown com exatamente estas secções:

## Resumo
(3 a 5 frases sobre o objetivo e o resultado da reunião)

## Principais pontos discutidos
(lista de tópicos)

## Decisões tomadas
(lista; escreve "Nenhuma decisão explícita." se não houver)

## Próximos passos / tarefas
(lista no formato "- [ ] tarefa — responsável (se for mencionado) — prazo (se for mencionado)")

## Questões em aberto
(lista; escreve "Nenhuma." se não houver)

Regras: usa apenas informação que está na transcrição, não inventes nomes, datas nem números.
Sê conciso.

TRANSCRIÇÃO:
\"\"\"
{texto}
\"\"\"
"""

PROMPT_NOTAS = """Segue-se um excerto (parte {n} de {total}) da transcrição de uma reunião.
Extrai, em português, notas objetivas em tópicos: assuntos discutidos, decisões, tarefas
(com responsável e prazo, se mencionados), números e datas importantes e questões em aberto.
Não inventes nada que não esteja no texto.

EXCERTO:
\"\"\"
{texto}
\"\"\"
"""


def ollama(prompt: str, modelo: str, url: str, descarregar: bool = False) -> str:
    corpo = json.dumps({
        "model": modelo,
        "prompt": prompt,
        "stream": False,
        "think": False,                          # desliga o "modo raciocínio" (qwen3, etc.)
        "options": {"temperature": 0.2, "num_ctx": 16384},
        # Por defeito o Ollama mantém o modelo em VRAM 5 min; na última chamada
        # descarregamo-lo para a GPU ficar livre para o Whisper na próxima reunião.
        "keep_alive": 0 if descarregar else "5m",
    }).encode()
    pedido = urllib.request.Request(f"{url}/api/generate", data=corpo,
                                    headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(pedido, timeout=1800) as resposta:
            return json.loads(resposta.read())["response"].strip()
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
    """Divide o texto em blocos de ~tamanho caracteres sem cortar frases."""
    blocos, atual = [], ""
    for frase in texto.replace("? ", "?|").replace(". ", ".|").replace("! ", "!|").split("|"):
        if len(atual) + len(frase) > tamanho and atual:
            blocos.append(atual.strip())
            atual = ""
        atual += frase + " "
    if atual.strip():
        blocos.append(atual.strip())
    return blocos


def resumir(texto: str, modelo: str, url: str) -> str:
    if not texto.strip():
        return "_A transcrição está vazia — não foi detetada fala no áudio._"
    blocos = dividir(texto, TAMANHO_BLOCO)
    print(f"🤖 A gerar resumo com '{modelo}'"
          + (f" (reunião longa: {len(blocos)} partes)..." if len(blocos) > 1 else "..."))
    if len(blocos) > 1:
        # Reuniões longas: primeiro tira notas de cada parte, depois junta tudo.
        notas = []
        for i, bloco in enumerate(blocos, 1):
            print(f"    parte {i}/{len(blocos)}")
            notas.append(f"### Parte {i}\n" + ollama(
                PROMPT_NOTAS.format(n=i, total=len(blocos), texto=bloco), modelo, url))
        texto = "\n\n".join(notas)
    return ollama(PROMPT_RESUMO.format(texto=texto), modelo, url, descarregar=True)


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
    segmentos = transcrever(wav, args.whisper, args.idioma, args.cpu)
    texto = guardar_transcricao(segmentos, pasta)
    if args.sem_resumo:
        print(f"📁 Transcrição em: {pasta}")
    else:
        gerar_resumo(texto, pasta, args)


def gerar_resumo(texto: str, pasta: Path, args) -> None:
    resumo = resumir(texto, args.modelo, args.ollama_url)
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
        wav = para_wav(args.audio, pasta / "audio.wav")
        processar_audio(wav, pasta, args)
    elif args.comando == "resumir":
        linhas = args.transcricao.read_text(encoding="utf-8").splitlines()
        texto = " ".join(l.split("] ", 1)[-1] for l in linhas if l.strip())
        gerar_resumo(texto, args.transcricao.parent, args)


if __name__ == "__main__":
    main()
