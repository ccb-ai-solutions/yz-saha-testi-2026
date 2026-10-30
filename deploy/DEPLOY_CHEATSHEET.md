# Deploying an open-weights LLM on a dedicated VM — cheatsheet

One track for a cloud VM or an on-prem server. Tried end to end on 23 Oct 2026: Magistral Small 1.2 on an
OVHcloud t2-90 (2× V100S) with vLLM 0.18.1. The full worked example is `OVH_V100S_RUNBOOK.md`.

## The track

| # | Step | Done when |
|---|---|---|
| 1 | Size the model to the GPUs | weights ≤ ~75 % of total GPU memory |
| 2 | Get the machine | `ssh` works and `nvidia-smi` lists the GPUs |
| 3 | Prepare the OS | driver OK, build tools + `python3-dev` installed, working inside `tmux` |
| 4 | Download the weights | files complete on the local disk |
| 5 | Install the inference server | your GPU's architecture (e.g. `sm_70`) is in PyTorch's list |
| 6 | Start the server | `Application startup complete`, listening on 127.0.0.1 only |
| 7 | Smoke test | one real request gives a sensible answer |
| 8 | Give access | SSH tunnel or TLS proxy + API key; nothing but SSH/HTTPS exposed |
| 9 | Validate | quality vs a reference + speed at the expected load |
| 10 | Make it a service | auto-restart, logs, monitoring, pinned versions |
| 11 | Clean up / hand over | cloud: instance deleted · on-prem: runbook handed over |

## 1. Size

- **Weights** ≈ parameters (billions) × bytes per parameter: bf16/fp16 = 2, FP8 = 1, 4-bit ≈ 0.6.
  24B in bf16 ≈ 48 GB.
- **Total GPU memory ≥ 1.3 × weights.** The rest holds the KV cache (context length × parallel users).
- **The GPU generation decides precision and software:**

| GPU | Precision | Note |
|---|---|---|
| V100 (Volta) | fp16 only | pinned older stack (we used vLLM 0.18.1 + PyTorch 2.10); no FlashAttention |
| A100 (Ampere) | bf16 | the mainstream baseline |
| L40S, H100/H200 (Ada/Hopper) | + FP8 | |
| RTX PRO 6000, B200 (Blackwell) | + FP4 | needs recent software |

- **Engine:**
  - **vLLM** is the default: OpenAI-compatible API and the best throughput.
  - **llama.cpp** suits GGUF quantised weights and old GPUs or CPU.
  - **Ollama** is for quick tests, not multi-user serving.

## 2. Machine

- **Cloud:**
  - a GPU flavour you have quota for, in a region that suits the data residency;
  - Ubuntu LTS image;
  - an SSH key whose *private* half is on your PC;
  - public IPv4, only port 22 open;
  - **hourly** billing.
- **On-prem:**
  - a GPU server (PCIe vs NVLink, power, cooling);
  - Ubuntu LTS;
  - SSH from the admin network and an internal DNS name.
  - No internet? See "Air-gapped" below.

## 3. OS

```bash
sudo apt update && sudo apt install -y tmux build-essential python3-venv python3-dev git
tmux new -s llm      # after a dropped SSH session: ssh in again, tmux attach -t llm
nvidia-smi           # GPUs + driver. No driver: `ubuntu-drivers list --gpgpu`, install the <branch>-server
                     # driver and nvidia-utils-<branch>-server, reboot
```

## 4. Weights

```bash
python3 -m venv ~/hf && ~/hf/bin/pip install -U "huggingface_hub[cli]"
~/hf/bin/hf download <org>/<model> --local-dir ~/models/<model>      # gated model: ~/hf/bin/hf auth login first
```

## 5. Server

```bash
python3 -m venv ~/vllm && ~/vllm/bin/pip install "vllm==<version>"     # always pin the version
~/vllm/bin/python -c "import torch; print(torch.cuda.get_arch_list(), torch.cuda.get_device_name(0))"
```

Write down the driver, CUDA, PyTorch and vLLM versions: they are part of the result.

**Container instead:** install the NVIDIA Container Toolkit, then:
`docker run --gpus all --ipc=host -p 127.0.0.1:8000:8000 -v ~/models:/models vllm/vllm-openai:v<version> --model /models/<model> …`

## 6. Start

```bash
openssl rand -hex 24 > ~/token.txt && chmod 600 ~/token.txt
~/vllm/bin/vllm serve ~/models/<model> --served-model-name <name> \
  --tensor-parallel-size <number of GPUs> --max-model-len 16384 --gpu-memory-utilization 0.90 \
  --host 127.0.0.1 --port 8000 --api-key "$(cat ~/token.txt)" \
  <flags from the model card: tokenizer mode, tool-call parser (reasoning parser only if the model thinks);
   --dtype half on V100/T4>
```

## 7. Smoke test (on the server)

```bash
curl -sf localhost:8000/health && echo healthy
curl -s http://127.0.0.1:8000/v1/chat/completions -H "Authorization: Bearer $(cat ~/token.txt)" \
  -H "Content-Type: application/json" \
  -d '{"model":"<name>","max_tokens":200,"messages":[{"role":"user","content":"Merhaba! Kendini tek cümleyle tanıt."}]}'
```

## 8. Access

- **Just you:** an SSH tunnel from your PC, `ssh -N -L 8000:127.0.0.1:8000 <user>@<ip>`. Then use
  `http://127.0.0.1:8000/v1` with the token.
- **Team / production:**
  - a reverse proxy (nginx or Caddy) with TLS in front;
  - one API key per application;
  - firewall open only to known networks.
  - Never open port 8000 itself.

## 9. Validate

- **Quality:** run the same test set against the new endpoint and a reference: the same weights hosted elsewhere,
  or the previous version. In trbench that is `probe` → `smoke` → battery. Check that tool calls and reasoning
  come back as structured fields, not just as text.
- **Speed:** measure time to first token and tokens/s at the concurrency you expect (trbench `latency`).

## 10. Service

```ini
# /etc/systemd/system/vllm.service  —  then: sudo systemctl enable --now vllm
[Unit]
After=network-online.target

[Service]
User=ubuntu
EnvironmentFile=/etc/vllm.env          # one line: VLLM_API_KEY=<token>   (chmod 600)
ExecStart=/home/ubuntu/vllm/bin/vllm serve /home/ubuntu/models/<model> --host 127.0.0.1 --port 8000 <flags>
Restart=always

[Install]
WantedBy=multi-user.target
```

- **Logs:** `journalctl -u vllm -f`.
- **Health:** `/health`. **Metrics:** `/metrics` (Prometheus), plus GPU metrics from NVIDIA's DCGM exporter.
- **Prompts** are not logged unless you add `--enable-log-requests`. Agree the retention with the client (KVKK).
- **Updates:** test a new pinned version on a copy first.

## 11. Clean up / hand over

- **Cloud:** stop the server and **delete the instance**, plus any volumes and floating IPs. Billing runs as long as
  it exists.
- **On-prem:** hand over the runbook: versions, flags, token handling and how to restart.

## Air-gapped on-prem

1. On a connected machine with the same OS and Python version, download the server and the weights:
   `pip download "vllm==<version>" -d wheels/` and `hf download … --local-dir model/`. Alternatively, pull the
   container image and `docker save` it.
2. Copy both to the server.
3. Install: `pip install --no-index --find-links wheels/ "vllm==<version>"`.

## Problems we hit (23 Oct) and the fixes

| Symptom | Fix |
|---|---|
| `ssh` hangs | that was the private `10.x` address; use the instance's public IPv4 |
| purple "Daemons using outdated libraries" screen | press Enter |
| server fails while compiling, `Python.h` missing | `sudo apt install -y python3-dev` (Triton builds a small C helper at run time) |
| "Free memory … is less than desired GPU memory utilization" | a crashed run left processes on the GPUs: `sudo fuser -k /dev/nvidia*` |
| "Bfloat16 is only supported on GPUs with compute capability of at least 8.0" | `--dtype half` |
| tool calls arrive as plain text (`[TOOL_CALLS]name{…}` in the answer) | vLLM 0.18.1: drop `--reasoning-parser` when the model does not think — the tool parser waits for a `[/THINK]` block; otherwise check the model card's tool-call flags |
| `curl` fails in PowerShell | run it on the server, or use `curl.exe` on Windows |
