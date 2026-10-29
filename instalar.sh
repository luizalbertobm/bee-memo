#!/usr/bin/env bash
# Instala tudo o que a Reunião Local precisa (Ubuntu / Zorin OS / Debian).
# O Ollama corre em Docker, por isso o Docker tem de estar instalado.
set -e
cd "$(dirname "$0")"
MODELO="qwen3:8b"

if ! docker info >/dev/null 2>&1; then
  echo "Erro: o Docker não está instalado ou o seu utilizador não tem acesso a ele."
  echo "Instale-o (https://docs.docker.com/engine/install/) e junte-se ao grupo 'docker'."
  exit 1
fi

echo "==> Pacotes do sistema (ffmpeg, PortAudio, venv)"
sudo apt update
sudo apt install -y ffmpeg libportaudio2 python3-venv python3-pip python3-gi gir1.2-gtk-3.0 curl

echo "==> Ambiente Python"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

COMPOSE_FILE="docker-compose.yml"
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "==> GPU NVIDIA detetada: a instalar bibliotecas CUDA para o Whisper"
  .venv/bin/pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"

  if ! docker info --format '{{json .Runtimes}}' | grep -q nvidia; then
    if ! command -v nvidia-ctk >/dev/null 2>&1; then
      echo "==> A instalar o nvidia-container-toolkit (GPU dentro do Docker)"
      curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
      curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
      sudo apt update
      sudo apt install -y nvidia-container-toolkit
    fi
    echo "    (o Docker vai ser reiniciado; contentores sem 'restart' ficam parados)"
    # O Docker rootless tem daemon próprio, configurado na pasta do utilizador
    if docker info --format '{{.SecurityOptions}}' | grep -q rootless; then
      nvidia-ctk runtime configure --runtime=docker --config="$HOME/.config/docker/daemon.json"
      sudo nvidia-ctk config --set nvidia-container-cli.no-cgroups --in-place
      systemctl --user restart docker
    else
      sudo nvidia-ctk runtime configure --runtime=docker
      sudo systemctl restart docker
    fi
    until docker info >/dev/null 2>&1; do sleep 1; done
  fi
  COMPOSE_FILE="docker-compose.yml:docker-compose.gpu.yml"
fi
echo "COMPOSE_FILE=$COMPOSE_FILE" > .env

# Um Ollama instalado localmente ocuparia a porta 11434
if systemctl is-active --quiet ollama 2>/dev/null; then
  echo "==> A parar o serviço Ollama local (passa a correr em Docker)"
  sudo systemctl disable --now ollama
fi

echo "==> Ollama em Docker"
docker compose up -d
until curl -fs http://localhost:11434/api/version >/dev/null; do sleep 1; done
docker exec reuniao-ollama ollama pull "$MODELO"

# Atalho 'reuniao' que trata das bibliotecas CUDA e arranca o Ollama se preciso
cat > reuniao <<'EOF'
#!/usr/bin/env bash
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
NV="$DIR/.venv/lib/python3*/site-packages/nvidia"
for lib in $NV/cublas/lib $NV/cudnn/lib; do
  [ -d "$lib" ] && export LD_LIBRARY_PATH="$lib:$LD_LIBRARY_PATH"
done
if ! curl -fs http://localhost:11434/api/version >/dev/null 2>&1; then
  (cd "$DIR" && docker compose up -d >/dev/null 2>&1) || true
fi
exec "$DIR/.venv/bin/python" "$DIR/reuniao.py" "$@"
EOF
chmod +x reuniao reuniao.py
mkdir -p ~/.local/bin && ln -sf "$PWD/reuniao" ~/.local/bin/reuniao

# Atalho no menu de aplicações (abre a interface gráfica)
mkdir -p ~/.local/share/applications
sed "s#@HOME@#$HOME#" reuniao.desktop > ~/.local/share/applications/reuniao.desktop
command -v update-desktop-database >/dev/null && update-desktop-database ~/.local/share/applications

echo
echo "✅ Instalado. Experimente:  reuniao gravar --titulo \"Teste\""
echo "   (se 'reuniao' não for encontrado, abra um terminal novo)"
