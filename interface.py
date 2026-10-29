#!/usr/bin/env python3
"""
Interface gráfica da Reunião Local — uma janela com um botão de gravar/terminar.

Não reimplementa o pipeline: constrói a linha de comandos e lança o reuniao.py
como subprocesso, mostrando o que ele imprime. Abre-se com `reuniao` sem argumentos.
"""
import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

PASTA = Path(__file__).resolve().parent
# O atalho `reuniao` configura as bibliotecas CUDA e arranca o Ollama; se existir, usamo-lo.
COMANDO = [str(PASTA / "reuniao")] if (PASTA / "reuniao").is_file() \
    else [str(PASTA / ".venv/bin/python"), str(PASTA / "reuniao.py")]
PREFERENCIAS = Path.home() / ".config" / "reuniao" / "interface.json"
NOTA_ONLINE = ("Entra na reunião no browser como habitualmente. Grava-se o microfone e o som do\n"
               "sistema (os outros participantes). Avisa-os de que a reunião está a ser gravada.")


def microfones() -> list[tuple[str, str]]:
    """[(valor para --dispositivo, texto a mostrar)], com o predefinido em primeiro."""
    opcoes = [("", "Predefinido do sistema")]
    try:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                opcoes.append((str(i), f"{i} — {d['name']}"))
    except Exception:
        pass
    return opcoes


class Janela:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.fila: "queue.Queue" = queue.Queue()
        self.pasta: str | None = None
        self.inicio = 0.0
        self.erro = ""
        self.gravacao_fechada = False
        self.mics = microfones()

        prefs = self.carregar_preferencias()
        self.titulo = tk.StringVar()
        self.modo = tk.StringVar(value=prefs.get("modo", "vivo"))
        self.gpu = tk.BooleanVar(value=prefs.get("gpu", shutil.which("nvidia-smi") is not None))
        self.mic = tk.StringVar(value=prefs.get("mic") or self.mics[0][1])
        if self.mic.get() not in [texto for _, texto in self.mics]:
            self.mic.set(self.mics[0][1])

        self.montar()
        self.root.protocol("WM_DELETE_WINDOW", self.fechar)
        self.root.after(100, self.tique)

    # ------------------------------------------------------------------ layout
    def montar(self):
        self.root.title("Reunião Local")
        self.root.minsize(560, 480)
        f = ttk.Frame(self.root, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Título").grid(row=0, column=0, sticky="w", pady=4)
        self.campo_titulo = ttk.Entry(f, textvariable=self.titulo)
        self.campo_titulo.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(f, text="Modo").grid(row=1, column=0, sticky="w", pady=4)
        modos = ttk.Frame(f)
        modos.grid(row=1, column=1, sticky="w")
        self.radio_vivo = ttk.Radiobutton(modos, text="Ao vivo (só microfone)", value="vivo",
                                          variable=self.modo, command=self.atualizar_modo)
        self.radio_online = ttk.Radiobutton(modos, text="Online (Meet, Teams, Zoom…)",
                                            value="online", variable=self.modo,
                                            command=self.atualizar_modo)
        self.radio_vivo.pack(side="left", padx=(0, 12))
        self.radio_online.pack(side="left")
        self.nota = ttk.Label(f, text=NOTA_ONLINE, foreground="#555", justify="left")

        ttk.Label(f, text="Microfone").grid(row=3, column=0, sticky="w", pady=4)
        self.combo_mic = ttk.Combobox(f, textvariable=self.mic, state="readonly",
                                      values=[texto for _, texto in self.mics])
        self.combo_mic.grid(row=3, column=1, sticky="ew", pady=4)

        self.check_gpu = ttk.Checkbutton(f, text="Usar GPU (transcrição mais rápida)",
                                         variable=self.gpu)
        self.check_gpu.grid(row=4, column=1, sticky="w", pady=4)

        acao = ttk.Frame(f)
        acao.grid(row=5, column=0, columnspan=2, pady=14)
        self.botao = tk.Button(acao, text="⏺  Iniciar gravação", font=("", 12, "bold"),
                               width=22, pady=6, command=self.alternar)
        self.botao.pack(side="left")
        self.cronometro = ttk.Label(acao, text="00:00", font=("", 12))
        self.cronometro.pack(side="left", padx=12)

        self.estado = ttk.Label(f, text="Pronto.", foreground="#555")
        self.estado.grid(row=6, column=0, columnspan=2, sticky="w")

        registo = ttk.Frame(f)
        registo.grid(row=7, column=0, columnspan=2, sticky="nsew", pady=(4, 8))
        f.rowconfigure(7, weight=1)
        self.texto = tk.Text(registo, height=12, wrap="word", font=("monospace", 10),
                             state="disabled", relief="solid", borderwidth=1)
        barra = ttk.Scrollbar(registo, command=self.texto.yview)
        self.texto.configure(yscrollcommand=barra.set)
        self.texto.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")

        self.botao_pasta = ttk.Button(f, text="Abrir pasta", state="disabled",
                                      command=self.abrir_pasta)
        self.botao_pasta.grid(row=8, column=1, sticky="e")

        self.atualizar_modo()
        self.campo_titulo.focus()

    def atualizar_modo(self):
        if self.modo.get() == "online":
            self.nota.grid(row=2, column=1, sticky="w", pady=(0, 4))
            self.combo_mic.configure(state="disabled")      # o --meet usa o predefinido
        else:
            self.nota.grid_remove()
            self.combo_mic.configure(state="readonly")

    def definir_estado(self, texto: str, cor: str = "#555"):
        self.estado.configure(text=texto, foreground=cor)

    def escrever(self, linha: str, substituir: bool = False):
        self.texto.configure(state="normal")
        if substituir:
            self.texto.delete("end-2l", "end-1c")
        self.texto.insert("end", linha + "\n")
        self.texto.see("end")
        self.texto.configure(state="disabled")

    def bloquear_opcoes(self, bloquear: bool):
        estado = "disabled" if bloquear else "normal"
        for w in (self.campo_titulo, self.radio_vivo, self.radio_online, self.check_gpu):
            w.configure(state=estado)
        if bloquear:
            self.combo_mic.configure(state="disabled")
        else:
            self.atualizar_modo()

    # ------------------------------------------------------------- subprocesso
    def argumentos(self) -> list[str]:
        args = COMANDO + ["gravar"]
        if self.titulo.get().strip():
            args += ["--titulo", self.titulo.get().strip()]
        if not self.gpu.get():
            args.append("--cpu")
        if self.modo.get() == "online":
            args.append("--meet")
        else:
            valor = dict((texto, v) for v, texto in self.mics).get(self.mic.get(), "")
            if valor:
                args += ["--dispositivo", valor]
        return args

    def alternar(self):
        if self.proc is None:
            self.iniciar()
        else:
            self.terminar()

    def iniciar(self):
        self.guardar_preferencias()
        self.pasta, self.erro, self.gravacao_fechada = None, "", False
        self.texto.configure(state="normal")
        self.texto.delete("1.0", "end")
        self.texto.configure(state="disabled")
        self.botao_pasta.configure(state="disabled")
        try:
            self.proc = subprocess.Popen(
                self.argumentos(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=0, cwd=PASTA,
                # sem isto o Python filho guarda os prints num buffer até sair
                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        except OSError as erro:
            messagebox.showerror("Reunião Local", f"Não foi possível arrancar a gravação:\n{erro}")
            self.proc = None
            return
        threading.Thread(target=self.ler, daemon=True).start()
        self.bloquear_opcoes(True)
        self.botao.configure(text="A iniciar…", state="disabled")
        self.definir_estado("A preparar a gravação…")

    def terminar(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)        # o mesmo que Ctrl+C no terminal
        self.botao.configure(text="A processar…", state="disabled", bg=self.root.cget("bg"))
        self.definir_estado("A terminar a gravação…")

    def ler(self):
        """Thread: lê a saída do processo e separa em linhas por \\n e por \\r."""
        anterior = "\n"
        atual = ""
        while True:
            c = self.proc.stdout.read(1)
            if not c:
                break
            if c in "\r\n":
                self.fila.put((anterior == "\r", atual))
                anterior, atual = c, ""
            else:
                atual += c
        if atual:
            self.fila.put((anterior == "\r", atual))
        self.proc.wait()
        self.fila.put(None)

    def tique(self):
        self.drenar()
        if self.proc and self.inicio and self.botao.cget("text").startswith("⏹"):
            seg = int(time.time() - self.inicio)
            self.cronometro.configure(text=f"{seg // 60:02d}:{seg % 60:02d}")
        self.root.after(100, self.tique)

    def drenar(self):
        while True:
            try:
                item = self.fila.get_nowait()
            except queue.Empty:
                return
            if item is None:
                self.concluir()
                return
            substituir, linha = item
            if "HF Hub" in linha:                        # aviso irrelevante do Hugging Face
                continue
            self.escrever(linha.rstrip(), substituir)
            self.interpretar(linha)

    def interpretar(self, linha: str):
        if linha.startswith("🎙"):
            # "🎙️  A gravar para <pasta>/audio.wav" — fica já a pasta, para o botão
            # "Abrir pasta" funcionar mesmo que a transcrição ou o resumo falhem
            if " para " in linha:
                self.pasta = str(Path(linha.split(" para ", 1)[1].strip()).parent)
            self.inicio = time.time()
            self.botao.configure(text="⏹  Terminar reunião", state="normal",
                                 bg="#c62828", fg="white", activebackground="#b71c1c")
            self.definir_estado("A gravar. Carrega em Terminar quando a reunião acabar.",
                                "#c62828")
        elif linha.startswith("✅"):
            self.gravacao_fechada = True
            self.botao.configure(text="A processar…", state="disabled",
                                 bg=self.root.cget("bg"), fg="black")
            self.definir_estado("A transcrever…")
        elif linha.startswith("🤖"):
            self.definir_estado("A gerar o resumo…")
        elif linha.startswith("📁"):
            self.pasta = linha.split(":", 1)[1].strip()
        elif "GPU indisponível" in linha:
            self.definir_estado("GPU indisponível — a usar o CPU (mais lento).", "#e65100")
        elif linha.startswith("Erro:"):
            self.erro = linha

    def concluir(self):
        codigo = self.proc.returncode if self.proc else 1
        self.proc = None
        self.bloquear_opcoes(False)
        self.botao.configure(text="⏺  Iniciar gravação", state="normal",
                             bg=self.root.cget("bg"), fg="black")
        if self.pasta:
            self.botao_pasta.configure(state="normal")
        if codigo == 0:
            self.definir_estado(f"Concluído. Ficheiros em {self.pasta}", "#2e7d32")
        else:
            detalhe = self.erro or "ver o registo acima"
            self.definir_estado("Falhou — " + detalhe, "#c62828")
            if self.root.winfo_exists():
                messagebox.showerror("Reunião Local", detalhe)

    def abrir_pasta(self):
        if self.pasta:
            subprocess.Popen(["xdg-open", self.pasta])

    # ------------------------------------------------------------------ fecho
    def fechar(self):
        if self.proc and self.proc.poll() is None:
            if not self.gravacao_fechada:
                if not messagebox.askyesno(
                        "Reunião Local",
                        "A reunião ainda está a ser gravada. Terminar a gravação?\n\n"
                        "O áudio fica guardado; podes processá-lo depois com\n"
                        "    reuniao processar <pasta>/audio.wav"):
                    return
                self.proc.send_signal(signal.SIGINT)
                # Espera que a gravação feche o WAV e limpe o áudio do sistema.
                limite = time.time() + 5
                while (time.time() < limite and not self.gravacao_fechada
                       and self.proc.poll() is None):
                    self.drenar()
                    time.sleep(0.1)
            elif not messagebox.askyesno(
                    "Reunião Local",
                    "A transcrição ou o resumo ainda estão a decorrer. Fechar mesmo assim?\n\n"
                    "O áudio fica guardado; podes retomar com\n"
                    "    reuniao processar <pasta>/audio.wav"):
                return
            if self.proc.poll() is None:
                self.proc.terminate()
        self.guardar_preferencias()
        self.root.destroy()

    # ------------------------------------------------------------ preferências
    def carregar_preferencias(self) -> dict:
        try:
            return json.loads(PREFERENCIAS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def guardar_preferencias(self):
        try:
            PREFERENCIAS.parent.mkdir(parents=True, exist_ok=True)
            PREFERENCIAS.write_text(json.dumps(
                {"modo": self.modo.get(), "gpu": self.gpu.get(), "mic": self.mic.get()},
                ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass


def main():
    root = tk.Tk()
    if "clam" in ttk.Style().theme_names():       # aspeto mais limpo que o Tk clássico
        ttk.Style().theme_use("clam")
    Janela(root)
    root.mainloop()


if __name__ == "__main__":
    main()
