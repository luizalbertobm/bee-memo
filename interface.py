#!/usr/bin/python3
"""
Interface gráfica da Reunião Local, em GTK 3 — o toolkit do ambiente de trabalho
(Zorin OS / GNOME), por isso segue o tema, a fonte, os ícones e o modo escuro do sistema.

Não reimplementa o pipeline: constrói a linha de comandos e lança o reuniao.py
como subprocesso, mostrando o que ele imprime. Abre-se com `reuniao` sem argumentos.
Corre com o Python do sistema (precisa de python3-gi), não com o do .venv.
"""
import codecs
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

PASTA = Path(__file__).resolve().parent
# O atalho `reuniao` configura as bibliotecas CUDA e arranca o Ollama; se existir, usamo-lo.
COMANDO = [str(PASTA / "reuniao")] if (PASTA / "reuniao").is_file() \
    else [str(PASTA / ".venv/bin/python"), str(PASTA / "reuniao.py")]
PREFERENCIAS = Path.home() / ".config" / "reuniao" / "interface.json"
ICONE = "audio-input-microphone"
DESCRICAO_MODO = {
    "vivo": "Grava só o microfone — reuniões presenciais.",
    "online": "Grava o microfone e o som do sistema (Meet, Teams, Zoom…). "
              "Avisa os participantes de que a reunião está a ser gravada.",
}


def microfones() -> list[tuple[str, str]]:
    """[(nome da fonte PulseAudio, descrição)], com o predefinido do sistema em primeiro."""
    opcoes = [("", "Predefinido do sistema")]
    try:
        saida = subprocess.run(["pactl", "list", "sources"], capture_output=True, text=True,
                               env={**os.environ, "LC_ALL": "C"}, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return opcoes
    nome = None
    for linha in saida.splitlines():
        linha = linha.strip()
        if linha.startswith("Name:"):
            nome = linha.split(":", 1)[1].strip()
        elif linha.startswith("Description:") and nome and not nome.endswith(".monitor"):
            opcoes.append((nome, linha.split(":", 1)[1].strip()))
    return opcoes


def linha_de_preferencias(titulo: str, subtitulo: str, controlo: Gtk.Widget,
                          expandir: bool = False) -> tuple[Gtk.ListBoxRow, Gtk.Label]:
    """Linha no estilo das Definições do GNOME: título (+ subtítulo) à esquerda, controlo à direita."""
    caixa = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
    textos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
    textos.pack_start(Gtk.Label(label=titulo, xalign=0), False, False, 0)
    sub = Gtk.Label(label=subtitulo, xalign=0, wrap=True, max_width_chars=40)
    sub.get_style_context().add_class("dim-label")
    sub.set_no_show_all(not subtitulo)
    textos.pack_start(sub, False, False, 0)
    caixa.pack_start(textos, not expandir, True, 0)
    controlo.set_valign(Gtk.Align.CENTER)
    caixa.pack_end(controlo, expandir, expandir, 0)
    linha = Gtk.ListBoxRow(activatable=False, selectable=False)
    linha.add(caixa)
    return linha, sub


class Janela(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="Reunião Local", icon_name=ICONE)
        self.set_default_size(560, 640)
        self.proc: subprocess.Popen | None = None
        self.pasta: str | None = None
        self.erro = ""
        self.inicio = 0.0
        self.gravacao_fechada = False
        self.a_gravar = False
        self.ultima_substituivel = False
        self.mics = microfones()
        self.montar(self.carregar_preferencias())
        self.connect("delete-event", self.ao_fechar)
        GLib.timeout_add(500, self.tique)

    # ------------------------------------------------------------------ layout
    def montar(self, prefs: dict):
        barra = Gtk.HeaderBar(title="Reunião Local", show_close_button=True)
        barra.set_subtitle("Pronto")
        self.barra = barra
        self.botao_pasta = Gtk.Button.new_from_icon_name("folder-open-symbolic",
                                                         Gtk.IconSize.BUTTON)
        self.botao_pasta.set_tooltip_text("Abrir a pasta da reunião")
        self.botao_pasta.set_sensitive(False)
        self.botao_pasta.connect("clicked", lambda *_: self.abrir_pasta())
        barra.pack_end(self.botao_pasta)
        self.set_titlebar(barra)

        corpo = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                        margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)

        # Opções, como uma lista de preferências do GNOME
        lista = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lista.set_header_func(self.separador)
        moldura = Gtk.Frame()
        moldura.add(lista)

        self.titulo = Gtk.Entry(placeholder_text="Opcional — usado no nome da pasta",
                                width_chars=28)
        lista.add(linha_de_preferencias("Título", "", self.titulo, expandir=True)[0])

        modos = Gtk.Box()
        modos.get_style_context().add_class("linked")
        self.radio_vivo = Gtk.RadioButton(label="Ao vivo", draw_indicator=False)
        self.radio_online = Gtk.RadioButton(label="Online", group=self.radio_vivo,
                                            draw_indicator=False)
        modos.pack_start(self.radio_vivo, False, False, 0)
        modos.pack_start(self.radio_online, False, False, 0)
        linha, self.descricao_modo = linha_de_preferencias("Modo", "", modos)
        lista.add(linha)

        self.combo_mic = Gtk.ComboBoxText()
        for nome, descricao in self.mics:
            self.combo_mic.append(nome or "predefinido", descricao)
        if not self.combo_mic.set_active_id(prefs.get("mic") or "predefinido"):
            self.combo_mic.set_active(0)
        celula = self.combo_mic.get_cells()[0]
        celula.set_property("ellipsize", Pango.EllipsizeMode.END)
        celula.set_property("width-chars", 24)
        lista.add(linha_de_preferencias("Microfone", "", self.combo_mic)[0])

        self.gpu = Gtk.Switch(active=prefs.get("gpu", shutil.which("nvidia-smi") is not None))
        lista.add(linha_de_preferencias(
            "Usar GPU", "Transcrição muito mais rápida (NVIDIA)", self.gpu)[0])
        corpo.pack_start(moldura, False, False, 0)

        # Botão principal
        conteudo = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self.icone_botao = Gtk.Image.new_from_icon_name("media-record-symbolic",
                                                        Gtk.IconSize.BUTTON)
        self.spinner = Gtk.Spinner(no_show_all=True)
        self.texto_botao = Gtk.Label(label="Iniciar gravação")
        conteudo.pack_start(self.icone_botao, False, False, 0)
        conteudo.pack_start(self.spinner, False, False, 0)
        conteudo.pack_start(self.texto_botao, False, False, 0)
        self.botao = Gtk.Button(halign=Gtk.Align.CENTER, width_request=240, height_request=44)
        self.botao.add(conteudo)
        self.botao.connect("clicked", lambda *_: self.alternar())
        self.definir_botao("iniciar")
        corpo.pack_start(self.botao, False, False, 0)

        self.estado = Gtk.Label(label="", xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER,
                                no_show_all=True)
        self.estado.get_style_context().add_class("dim-label")
        corpo.pack_start(self.estado, False, False, 0)

        # No fim: que ficheiro se vê no painel (Resumo | Transcrição) e o botão do áudio.
        # O rádio "registo" não aparece; serve para nenhum dos dois ficar selecionado
        # enquanto o painel mostra a saída do processo.
        self.resultados = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, no_show_all=True)
        vistas = Gtk.Box()
        vistas.get_style_context().add_class("linked")
        self.vista_registo = Gtk.RadioButton()
        self.vista_resumo = self.botao_vista("Resumo", "view-list-symbolic", "resumo.md")
        self.vista_transcricao = self.botao_vista("Transcrição", "text-x-generic-symbolic",
                                                  "transcricao.txt")
        vistas.pack_start(self.vista_resumo, False, False, 0)
        vistas.pack_start(self.vista_transcricao, False, False, 0)
        self.botao_audio = Gtk.Button(label="Áudio", always_show_image=True,
                                      tooltip_text="Ouvir a gravação no leitor do sistema")
        self.botao_audio.set_image(Gtk.Image.new_from_icon_name(
            "media-playback-start-symbolic", Gtk.IconSize.BUTTON))
        self.botao_audio.connect("clicked", lambda *_: self.abrir_ficheiro("audio.wav"))
        self.botao_copiar = Gtk.Button(label="Copiar", always_show_image=True,
                                       tooltip_text="Copiar o texto mostrado no painel",
                                       sensitive=False)
        self.botao_copiar.set_image(Gtk.Image.new_from_icon_name(
            "edit-copy-symbolic", Gtk.IconSize.BUTTON))
        self.botao_copiar.connect("clicked", lambda *_: self.copiar())
        self.resultados.pack_start(vistas, False, False, 0)
        self.resultados.pack_start(self.botao_copiar, False, False, 0)
        self.resultados.pack_start(self.botao_audio, False, False, 0)
        for filho in (vistas, self.vista_resumo, self.vista_transcricao, self.botao_copiar,
                      self.botao_audio):
            filho.show()
        # Só há o que copiar quando o painel mostra o resumo ou a transcrição, não o registo.
        self.vista_registo.connect(
            "toggled", lambda b: self.botao_copiar.set_sensitive(not b.get_active()))
        corpo.pack_start(self.resultados, False, False, 0)

        # Registo do processo e, no fim, o ficheiro escolhido
        self.texto = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                  wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=10,
                                  right_margin=10, top_margin=8, bottom_margin=8)
        rolagem = Gtk.ScrolledWindow(vexpand=True, min_content_height=160)
        rolagem.add(self.texto)
        moldura_texto = Gtk.Frame()
        moldura_texto.add(rolagem)
        corpo.pack_start(moldura_texto, True, True, 0)

        self.add(corpo)
        self.radio_online.set_active(prefs.get("modo") == "online")
        self.radio_vivo.connect("toggled", lambda *_: self.atualizar_modo())
        self.atualizar_modo()

    def botao_vista(self, rotulo: str, icone: str, ficheiro: str) -> Gtk.RadioButton:
        botao = Gtk.RadioButton(label=rotulo, group=self.vista_registo, draw_indicator=False,
                                always_show_image=True)
        botao.set_image(Gtk.Image.new_from_icon_name(icone, Gtk.IconSize.BUTTON))
        botao.connect("toggled", lambda b: b.get_active() and self.mostrar_ficheiro(ficheiro))
        return botao

    @staticmethod
    def separador(linha: Gtk.ListBoxRow, anterior: Gtk.ListBoxRow | None):
        if anterior and not linha.get_header():
            linha.set_header(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

    def modo(self) -> str:
        return "online" if self.radio_online.get_active() else "vivo"

    def atualizar_modo(self):
        self.descricao_modo.set_text(DESCRICAO_MODO[self.modo()])
        self.descricao_modo.show()

    def definir_botao(self, estado: str, texto: str = "A processar…"):
        """iniciar | parar | ocupado — usa as classes de estilo do tema (azul / vermelho)."""
        contexto = self.botao.get_style_context()
        contexto.remove_class("suggested-action")
        contexto.remove_class("destructive-action")
        if estado == "iniciar":
            contexto.add_class("suggested-action")
            self.icone_botao.set_from_icon_name("media-record-symbolic", Gtk.IconSize.BUTTON)
            self.texto_botao.set_text("Iniciar gravação")
        elif estado == "parar":
            contexto.add_class("destructive-action")
            self.icone_botao.set_from_icon_name("media-playback-stop-symbolic",
                                                Gtk.IconSize.BUTTON)
            self.texto_botao.set_text("Terminar reunião  00:00")
        else:
            self.texto_botao.set_text(texto)
        ocupado = estado == "ocupado"
        self.icone_botao.set_visible(not ocupado)
        self.spinner.set_visible(ocupado)
        self.spinner.start() if ocupado else self.spinner.stop()
        self.botao.set_sensitive(not ocupado)

    def definir_estado(self, subtitulo: str, detalhe: str = ""):
        self.barra.set_subtitle(subtitulo)
        self.estado.set_text(detalhe)
        self.estado.set_visible(bool(detalhe))

    def escrever(self, linha: str, substituir: bool = False):
        buffer = self.texto.get_buffer()
        if substituir and self.ultima_substituivel:
            inicio = buffer.get_iter_at_line(max(buffer.get_line_count() - 2, 0))
            buffer.delete(inicio, buffer.get_end_iter())
        buffer.insert(buffer.get_end_iter(), linha + "\n")
        self.ultima_substituivel = substituir
        self.texto.scroll_to_mark(buffer.get_insert(), 0, False, 0, 1)

    def bloquear_opcoes(self, bloquear: bool):
        for w in (self.titulo, self.radio_vivo, self.radio_online, self.combo_mic, self.gpu):
            w.set_sensitive(not bloquear)

    # ------------------------------------------------------------- subprocesso
    def argumentos(self) -> list[str]:
        args = COMANDO + ["gravar"]
        if self.titulo.get_text().strip():
            args += ["--titulo", self.titulo.get_text().strip()]
        if not self.gpu.get_active():
            args.append("--cpu")
        if self.modo() == "online":
            args.append("--meet")
        mic = self.combo_mic.get_active_id()
        if mic and mic != "predefinido":
            args += ["--dispositivo", mic]
        return args

    def alternar(self):
        if self.proc is None:
            self.iniciar()
        else:
            self.terminar()

    def iniciar(self):
        self.guardar_preferencias()
        self.pasta, self.erro, self.gravacao_fechada = None, "", False
        self.resultados.hide()
        self.vista_registo.set_active(True)
        self.texto.set_monospace(True)
        self.texto.get_buffer().set_text("")
        self.ultima_substituivel = False
        self.botao_pasta.set_sensitive(False)
        try:
            # Em bytes: em modo texto o Python converteria os \r em \n, e é pelos \r
            # que se distinguem as linhas que se atualizam no lugar (cronómetro, %).
            self.proc = subprocess.Popen(
                self.argumentos(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, cwd=PASTA,
                # sem isto o Python filho guarda os prints num buffer até sair
                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        except OSError as erro:
            self.mensagem(Gtk.MessageType.ERROR, "Não foi possível iniciar a gravação",
                          str(erro))
            self.proc = None
            return
        threading.Thread(target=self.ler, args=(self.proc,), daemon=True).start()
        self.bloquear_opcoes(True)
        self.definir_botao("ocupado", "A iniciar…")
        self.definir_estado("A preparar…")

    def terminar(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)        # o mesmo que Ctrl+C no terminal
        self.a_gravar = False
        self.definir_botao("ocupado")
        self.definir_estado("A terminar a gravação…")

    def ler(self, proc: subprocess.Popen):
        """Thread: lê a saída do processo e separa em linhas por \\n e por \\r."""
        descodificador = codecs.getincrementaldecoder("utf-8")(errors="replace")
        anterior, atual = "\n", ""
        while True:
            bloco = os.read(proc.stdout.fileno(), 4096)
            if not bloco:
                break
            for c in descodificador.decode(bloco):
                if c in "\r\n":
                    GLib.idle_add(self.receber, anterior == "\r", atual)
                    anterior, atual = c, ""
                else:
                    atual += c
        if atual:
            GLib.idle_add(self.receber, anterior == "\r", atual)
        proc.wait()
        GLib.idle_add(self.concluir)

    def receber(self, substituir: bool, linha: str):
        # aviso irrelevante do Hugging Face; a dica do Ctrl+C só faz sentido no terminal
        if linha.strip() and "HF Hub" not in linha and "Ctrl+C" not in linha:
            self.escrever(linha.rstrip(), substituir)
            self.interpretar(linha)
        return False

    def interpretar(self, linha: str):
        if linha.startswith("🎙"):
            # "🎙️  A gravar para <pasta>/audio.wav" — fica já a pasta, para o botão
            # da pasta funcionar mesmo que a transcrição ou o resumo falhem
            if " para " in linha:
                self.pasta = str(Path(linha.split(" para ", 1)[1].strip()).parent)
            self.inicio, self.a_gravar = time.time(), True
            self.definir_botao("parar")
            self.definir_estado("A gravar", "Carrega em Terminar quando a reunião acabar.")
        elif linha.startswith("✅"):
            self.gravacao_fechada, self.a_gravar = True, False
            self.definir_botao("ocupado")
            self.definir_estado("A transcrever…")
        elif linha.startswith("🤖"):
            self.definir_estado("A gerar o resumo…")
        elif linha.startswith("📁"):
            self.pasta = linha.split(":", 1)[1].strip()
        elif "GPU indisponível" in linha:
            self.definir_estado("A transcrever no CPU…",
                                "GPU indisponível — a transcrição vai demorar mais.")
        elif linha.startswith("Erro:"):
            self.erro = linha.removeprefix("Erro:").strip()

    def tique(self) -> bool:
        if self.a_gravar:
            seg = int(time.time() - self.inicio)
            h, resto = divmod(seg, 3600)
            tempo = f"{h}:{resto // 60:02d}:{resto % 60:02d}" if h else \
                f"{resto // 60:02d}:{resto % 60:02d}"
            self.texto_botao.set_text(f"Terminar reunião  {tempo}")
        return True

    def concluir(self):
        codigo = self.proc.returncode if self.proc else 1
        self.proc, self.a_gravar = None, False
        self.bloquear_opcoes(False)
        self.definir_botao("iniciar")
        self.botao_pasta.set_sensitive(bool(self.pasta))
        self.mostrar_resultados()
        if codigo == 0:
            self.definir_estado("Concluído")
            if self.vista_resumo.get_sensitive():
                self.vista_resumo.set_active(True)        # limpa o registo e mostra o resumo
            self.notificar("Resumo pronto", Path(self.pasta).name if self.pasta else "")
        else:
            # O registo fica no painel: é ele que explica o que falhou.
            detalhe = self.erro or "Vê os detalhes no registo."
            self.definir_estado("Falhou", detalhe)
            self.notificar("A reunião não foi processada", detalhe)
            self.mensagem(Gtk.MessageType.ERROR, "A reunião não foi processada", detalhe)
        return False

    def mostrar_resultados(self):
        """Mostra os botões dos ficheiros gerados, ativos só os que existem."""
        if not self.pasta:
            return
        pasta = Path(self.pasta)
        self.vista_resumo.set_sensitive((pasta / "resumo.md").exists())
        self.vista_transcricao.set_sensitive((pasta / "transcricao.txt").exists())
        self.botao_audio.set_sensitive((pasta / "audio.wav").exists())
        self.resultados.show()

    def mostrar_ficheiro(self, nome: str):
        """Substitui o conteúdo do painel pelo ficheiro da reunião."""
        try:
            conteudo = (Path(self.pasta) / nome).read_text(encoding="utf-8")
        except (OSError, TypeError):
            conteudo = f"Não foi possível abrir {nome}."
        # A transcrição tem [mm:ss] no início das linhas, que ficam alinhados em monospace;
        # o resumo é prosa e lê-se melhor na fonte do sistema.
        self.texto.set_monospace(nome == "transcricao.txt")
        buffer = self.texto.get_buffer()
        buffer.set_text(conteudo.rstrip() + "\n")
        self.ultima_substituivel = False
        buffer.place_cursor(buffer.get_start_iter())
        self.texto.scroll_to_iter(buffer.get_start_iter(), 0, False, 0, 0)

    def copiar(self):
        """Copia para a área de transferência o ficheiro que está no painel."""
        buffer = self.texto.get_buffer()
        conteudo = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        area = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        area.set_text(conteudo, -1)
        area.store()                          # fica disponível mesmo depois de fechar a janela
        self.botao_copiar.set_label("Copiado")
        self.botao_copiar.get_image().set_from_icon_name("object-select-symbolic",
                                                         Gtk.IconSize.BUTTON)
        GLib.timeout_add(1500, self.repor_copiar)

    def repor_copiar(self) -> bool:
        self.botao_copiar.set_label("Copiar")
        self.botao_copiar.get_image().set_from_icon_name("edit-copy-symbolic",
                                                         Gtk.IconSize.BUTTON)
        return False

    def abrir_ficheiro(self, nome: str):
        """Abre um ficheiro da reunião na aplicação predefinida do sistema."""
        if self.pasta:
            Gtk.show_uri_on_window(self, (Path(self.pasta) / nome).as_uri(),
                                   Gtk.get_current_event_time())

    def abrir_pasta(self):
        if self.pasta:
            Gtk.show_uri_on_window(self, Path(self.pasta).as_uri(), Gtk.get_current_event_time())

    def notificar(self, titulo: str, corpo: str):
        """Notificação do sistema — útil quando a janela está escondida atrás do browser."""
        if not self.is_active() and shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "Reunião Local", "-i", ICONE, titulo, corpo])

    def mensagem(self, tipo: Gtk.MessageType, titulo: str, texto: str,
                 botoes: Gtk.ButtonsType = Gtk.ButtonsType.CLOSE) -> Gtk.ResponseType:
        dialogo = Gtk.MessageDialog(transient_for=self, modal=True, message_type=tipo,
                                    buttons=botoes, text=titulo)
        dialogo.format_secondary_text(texto)
        resposta = dialogo.run()
        dialogo.destroy()
        return resposta

    # ------------------------------------------------------------------ fecho
    def ao_fechar(self, *_) -> bool:
        """Devolve True para impedir o fecho."""
        if self.proc and self.proc.poll() is None:
            if not self.gravacao_fechada:
                titulo = "Terminar a gravação?"
                texto = ("A reunião ainda está a ser gravada. O áudio fica guardado e podes "
                         "processá-lo depois com «reuniao processar <pasta>/audio.wav».")
            else:
                titulo = "Fechar durante o processamento?"
                texto = ("A transcrição ou o resumo ainda estão a decorrer. O áudio fica "
                         "guardado; podes retomar com «reuniao processar <pasta>/audio.wav».")
            if self.mensagem(Gtk.MessageType.QUESTION, titulo, texto,
                             Gtk.ButtonsType.YES_NO) != Gtk.ResponseType.YES:
                return True
            if not self.gravacao_fechada:
                self.proc.send_signal(signal.SIGINT)
                # Espera que a gravação feche o WAV e limpe o áudio do sistema.
                limite = time.time() + 5
                while (time.time() < limite and not self.gravacao_fechada
                       and self.proc.poll() is None):
                    while Gtk.events_pending():
                        Gtk.main_iteration()
                    time.sleep(0.05)
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        self.guardar_preferencias()
        return False

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
                {"modo": self.modo(), "gpu": self.gpu.get_active(),
                 "mic": self.combo_mic.get_active_id()},
                ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass


def main():
    GLib.set_prgname("reuniao")                   # liga a janela ao reuniao.desktop
    Gdk.set_program_class("reuniao")              # (StartupWMClass) para a dock agrupar o ícone
    GLib.set_application_name("Reunião Local")
    app = Gtk.Application(application_id="local.reuniao.Gravador")

    def ativar(app):
        # Uma só janela: abrir a app de novo traz a existente para a frente.
        janela = app.get_active_window()
        if janela:
            janela.present()
        else:
            Janela(app).show_all()

    app.connect("activate", ativar)
    app.run(None)


if __name__ == "__main__":
    main()
