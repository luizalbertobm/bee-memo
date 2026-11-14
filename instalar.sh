#!/usr/bin/env bash
# Instala tudo o que o Bee Memo precisa (Ubuntu / Zorin OS / Debian).
# O Ollama corre em Docker, por isso o Docker tem de estar instalado.
set -e
cd "$(dirname "$0")"
MODELO="qwen3.5:4b"
# Versão CUDA do sherpa-onnx (identificação de quem fala na GPU); a versão tem de ser a
# mesma do requirements.txt e compilada para CUDA 12 + cuDNN 9, como o Whisper.
SHERPA_CUDA="https://huggingface.co/csukuangfj2/sherpa-onnx-wheels/resolve/main/cuda/2.0.1/sherpa_onnx-2.0.1+cuda12.cudnn9.onnxruntime1.28.2-cp310-cp310-linux_x86_64.whl"

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
  echo "==> GPU NVIDIA detetada: a instalar bibliotecas CUDA (Whisper e identificação de quem fala)"
  .venv/bin/pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" \
    nvidia-cuda-runtime-cu12 nvidia-cufft-cu12 nvidia-curand-cu12
  .venv/bin/pip install --no-deps --force-reinstall "$SHERPA_CUDA"

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

# Instalações anteriores, com o nome "reuniao": passa as preferências, os modelos de oradores
# e os modelos do Ollama (~13 GB) para os nomes novos, em vez de os descarregar de novo.
[ -d ~/.config/reuniao ] && [ ! -e ~/.config/bee-memo ] && mv ~/.config/reuniao ~/.config/bee-memo
[ -d ~/.cache/reuniao ] && [ ! -e ~/.cache/bee-memo ] && mv ~/.cache/reuniao ~/.cache/bee-memo
rm -f ~/.local/bin/reuniao ~/.local/share/applications/reuniao.desktop
if docker inspect reuniao-ollama >/dev/null 2>&1; then
  ANTIGO=$(docker inspect reuniao-ollama \
    --format '{{range .Mounts}}{{if eq .Destination "/root/.ollama"}}{{.Name}}{{end}}{{end}}')
  docker rm -f reuniao-ollama >/dev/null
  if [ -n "$ANTIGO" ] && ! docker volume inspect bee-memo_ollama >/dev/null 2>&1; then
    echo "==> A passar os modelos do Ollama para o volume bee-memo_ollama"
    # com as etiquetas do Compose, para ele o reconhecer como volume do projeto
    docker volume create --label com.docker.compose.project=bee-memo \
      --label com.docker.compose.volume=ollama bee-memo_ollama >/dev/null
    docker run --rm -v "$ANTIGO":/de -v bee-memo_ollama:/para --entrypoint sh ollama/ollama \
      -c 'cp -a /de/. /para/'
    echo "    O volume antigo pode ser apagado com:  docker volume rm $ANTIGO"
  fi
fi

echo "==> Ollama em Docker"
docker compose up -d
until curl -fs http://localhost:11434/api/version >/dev/null; do sleep 1; done
docker exec bee-memo-ollama ollama pull "$MODELO"

# Atalho 'bee-memo' que trata das bibliotecas CUDA e arranca o Ollama se preciso
cat > bee-memo <<'EOF'
#!/usr/bin/env bash
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
NV="$DIR/.venv/lib/python3*/site-packages/nvidia"
for lib in $NV/cublas/lib $NV/cudnn/lib $NV/cuda_runtime/lib $NV/cufft/lib $NV/curand/lib; do
  [ -d "$lib" ] && export LD_LIBRARY_PATH="$lib:$LD_LIBRARY_PATH"
done
if ! curl -fs http://localhost:11434/api/version >/dev/null 2>&1; then
  (cd "$DIR" && docker compose up -d >/dev/null 2>&1) || true
fi
exec "$DIR/.venv/bin/python" "$DIR/bee_memo.py" "$@"
EOF
chmod +x bee-memo bee_memo.py
mkdir -p ~/.local/bin && ln -sf "$PWD/bee-memo" ~/.local/bin/bee-memo

# Atalho no menu de aplicações (abre a interface gráfica)
mkdir -p ~/.local/share/applications
sed "s#@HOME@#$HOME#" net.beecoders.BeeMemo.desktop \
  > ~/.local/share/applications/net.beecoders.BeeMemo.desktop
command -v update-desktop-database >/dev/null && update-desktop-database ~/.local/share/applications

echo
echo "✅ Instalado. Experimente:  bee-memo gravar --titulo \"Teste\""
echo "   (se 'bee-memo' não for encontrado, abra um terminal novo)"
