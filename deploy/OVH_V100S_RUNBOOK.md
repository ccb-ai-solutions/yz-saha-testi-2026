# Practice deployment: Magistral Small 1.2 on OVHcloud t2-90 (2× V100S 32 GB)

**Goal.** Practise the on-prem path end to end — GPU node, inference server, OpenAI-compatible endpoint, benchmark
— and measure **quality parity**: the same weights self-hosted vs hosted on Amazon Bedrock, on the same plans.

**What it does not show.** Speed on RTX PRO 6000 / H200. Volta (V100/V100S) has no bf16, no FP8 and no
FlashAttention-2, and current software has dropped it: vLLM ≥ 0.20 moved to PyTorch 2.11, whose CUDA 12.8 builds no
longer contain Volta kernels, and CUDA 13 removed Volta. Everything below therefore uses pinned versions — vLLM 0.18.1
with PyTorch 2.10.0, whose PyPI wheels still contain V100 kernels (checked 23 Oct) — verify each step instead of
assuming.

Time box: **~6 h** (≈1 h setup + 4–5 h of benchmark runs). Billing runs while the instance exists — **delete it at
the end**.

---

## 0. Before you start

- [ ] Phase 4 has run (the Bedrock twin `magistral-small-1.2` has results to compare with).
- [ ] An SSH key **of the PC you will connect from** is in your OVHcloud project. OVH keeps only the public half; the
      private half must be on that PC. On Windows: `Test-Path $env:USERPROFILE\.ssh\id_ed25519.pub` — if `False`, run
      `ssh-keygen -t ed25519` (Enter through the prompts); then `Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub |
      Set-Clipboard` and paste that one line into the wizard's "Add a key".
- [ ] You have ~6 hours in one go (or snapshot / delete in between); the PC stays on, plugged in, lid open.

## 1. Create the instance

OVHcloud Control Panel → Public Cloud → Instances → Create:

- **Model:** `t2-90` (2× Tesla V100S 32 GB; €1.60/h in the panel on 23 Oct — the GPU credits cover t1/t2).
  `t2-le-90` has the same GPUs. `t2-180` (4 GPUs) is the fallback if memory is tight; `t2-45` (1 GPU) needs 8-bit
  weights (Plan B).
- **Region:** any that offers t2-90; a European one gives a shorter round trip for the latency step.
- **Image:** Ubuntu 22.04 or 24.04.
- **SSH key:** the PC's key (step 0).
- **Network:** public mode (a public IP). Nothing else to open: the model server listens on `127.0.0.1` only and is
  reached through an SSH tunnel — never start it with `--host 0.0.0.0`.
- **Billing:** **hourly**. Monthly billing commits you to the rest of the month even if the instance is deleted.

## 2. Driver and CUDA (on the instance)

From the PC: `ssh ubuntu@<public IP>` (answer `yes` to the fingerprint question). Then:

```bash
sudo apt update && sudo apt install -y tmux build-essential git python3-venv python3-dev ubuntu-drivers-common
# (a "Daemons using outdated libraries" screen may appear: keep the ticks, press Enter)
tmux new -s trb                          # work inside tmux: a dropped SSH connection then stops nothing
                                         # (reconnect: ssh ubuntu@<IP>, then `tmux attach -t trb`;
                                         #  new window: Ctrl+B then C; switch: Ctrl+B then 0 / 1 / 2)
nvidia-smi || true                       # skip the next 4 lines if it shows 2x V100S (OVH's Ubuntu 22.04 image
                                         # had driver 580.178.04 preinstalled on 23 Oct)
sudo ubuntu-drivers list --gpgpu         # pick a 580-series *server* driver: newer branches drop Volta
sudo ubuntu-drivers install --gpgpu nvidia:580-server
sudo apt install -y nvidia-utils-580-server                # nvidia-smi (the --gpgpu driver comes without it)
sudo reboot
# after the reboot (1-2 min): ssh in again, `tmux new -s trb`, then:
nvidia-smi                               # expect: 2x Tesla V100S-PCIE-32GB, driver 580.x, "CUDA Version 12.x/13.x"
nvidia-smi topo -m                       # NV# = NVLink between the GPUs; PHB/SYS = PCIe only (TP=2 still works)
```

## 3. Weights

```bash
df -h ~                                  # need ~80 GB free (weights ~48 GB + vLLM/PyTorch ~10 GB)
python3 -m venv ~/venv && source ~/venv/bin/activate && pip install -U pip "huggingface_hub[cli]"
hf download mistralai/Magistral-Small-2509 --include "consolidated*" --include "*.json" --include "*.txt" \
  --local-dir ~/models/magistral-small-2509            # Mistral-format files only (what vLLM loads), Apache-2.0
ls -la ~/models/magistral-small-2509     # consolidated.safetensors ≈ 48 GB, params.json, tekken.json
```

The download takes a few minutes; do step 4's install in a second tmux window (Ctrl+B then C) meanwhile.

---

## 4. Plan A — vLLM (preferred: same tokenizer path as Mistral's reference)

vLLM 0.18.1 is new enough for Magistral 2509 (it requires `mistral_common >= 1.10`; the model needs ≥ 1.8.5) and
still runs on Volta: it pins PyTorch 2.10.0, whose CUDA 12.8 build on PyPI still contains sm_70 kernels (PyTorch
dropped Volta from its 12.8 builds in 2.11, which vLLM ≥ 0.20 uses), and the 0.18.1 wheel's own kernels include
sm_70 too (both checked on 23 Oct by listing the GPU architectures embedded in the wheels).

```bash
python3 -m venv ~/vllm && source ~/vllm/bin/activate && pip install -U pip
pip install "vllm==0.18.1"               # from PyPI; pulls torch 2.10.0+cu128 (≈ 10 GB with its CUDA libraries)
python - <<'EOF'
import torch, vllm
print("torch", torch.__version__, "cuda", torch.version.cuda, "arch", torch.cuda.get_arch_list(), "vllm", vllm.__version__)
assert "sm_70" in torch.cuda.get_arch_list(), "this PyTorch build has no Volta kernels"
x = torch.randn(1024, 1024, device="cuda", dtype=torch.half)
print("fp16 matmul on", torch.cuda.get_device_name(0), "ok:", bool(torch.isfinite(x @ x).all()))
EOF
```

**Go / no-go 1:** `sm_70` is in the arch list and the matmul prints `ok: True`. If not, go to Plan B (building
vLLM from source for sm_70 takes well over an hour).

Make the server token (it becomes `OVH_DEPLOY_TOKEN` in `.env` on your PC, step 6) and start the server — once the
weights are complete:

```bash
openssl rand -hex 24 > ~/token.txt && chmod 600 ~/token.txt && cat ~/token.txt     # copy this line for step 6
vllm serve ~/models/magistral-small-2509 \
  --served-model-name magistral-small-1.2 \
  --tokenizer-mode mistral --config-format mistral --load-format mistral \
  --tool-call-parser mistral --enable-auto-tool-choice \
  --dtype half --tensor-parallel-size 2 \
  --max-model-len 16384 --gpu-memory-utilization 0.92 \
  --limit-mm-per-prompt '{"image":0}' \
  --host 127.0.0.1 --port 8000 --api-key "$(cat ~/token.txt)" 2>&1 | tee ~/vllm.log
```

- **No `--reasoning-parser mistral` for the parity run** (found 23 Oct): with it, vLLM 0.18.1 hands a streamed
  answer to the tool-call parser only after a `[/THINK]` block. Magistral without its reasoning system prompt never
  writes one, so every tool call came back as plain text (`[TOOL_CALLS]name{…}` in the content) — exactly what
  Bedrock returns too. Add the flag only when you run Magistral with Mistral's reasoning system prompt.
- Start-up takes several minutes (48 GB to load, then compilation and CUDA-graph capture); it is ready at
  `Application startup complete`. The log should name the attention backend: `Using TRITON_ATTN attention backend`
  (FlashAttention needs compute capability 8.0; Triton is chosen automatically on Volta).
- `--dtype half`: Volta has no bf16; the bf16 weights are cast to fp16 (logged as a warning).
- `--limit-mm-per-prompt '{"image":0}'`: text only, so the vision encoder takes no extra memory.
- 16k context is plenty (RAG prompt ≈ 2.2k + the answer) and leaves KV room for several parallel requests.

**If startup fails:**

| Symptom | Try |
|---|---|
| `Python.h: No such file or directory` / `CalledProcessError` from gcc during compilation (23 Oct) | `sudo apt install -y python3-dev` — Triton builds a small C launcher at run time and needs Python's headers |
| "no kernel image is available" / unsupported compute capability | the check above was skipped or failed → Plan B |
| attention backend error on compute capability 7.0 | add `--attention-backend TRITON_ATTN` |
| hangs at start-up or in all-reduce (PCIe, virtualised GPUs) | add `--disable-custom-all-reduce`; still stuck → prefix the command with `NCCL_P2P_DISABLE=1` |
| Triton / compilation error, or CUDA-graph capture fails | add `--enforce-eager` (slower, simpler) |
| OOM at start | lower `--max-model-len` to 12288 or `--gpu-memory-utilization` to 0.90 |
| answers are garbage / NaN | fp16 overflow — stop and use Plan B (record it: it is a finding) |

**Go / no-go 2:** one Turkish request answers sensibly (from the instance, in a second tmux window):

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions -H "Authorization: Bearer $(cat ~/token.txt)" \
  -H "Content-Type: application/json" \
  -d '{"model":"magistral-small-1.2","max_tokens":4096,"messages":[{"role":"user","content":"İstanbul hangi iki kıtada yer alır? Tek cümleyle yanıtla."}]}'
```

With Mistral's reasoning system prompt (and then `--reasoning-parser mistral`), the reasoning arrives in
`reasoning_content` / `reasoning`, without the parser inline as `[THINK]…[/THINK]`; the harness handles both.

Optional, not for the parity test: **1Cat-vLLM** is a community fork focused on V100 (its own FlashAttention port,
recent models). It does not list Magistral and is not upstream — keep it for later experiments.

## 5. Plan B — llama.cpp (robust on Volta)

```bash
# CUDA toolkit 12.8 (NOT 13 — it dropped Volta) from NVIDIA's repository; the driver from step 2 stays as it is:
d=$(. /etc/os-release; echo ubuntu${VERSION_ID/./})
wget -q https://developer.download.nvidia.com/compute/cuda/repos/$d/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb && sudo apt update && sudo apt install -y cuda-toolkit-12-8 cmake
export PATH=/usr/local/cuda-12.8/bin:$PATH
[ -f ~/token.txt ] || { openssl rand -hex 24 > ~/token.txt && chmod 600 ~/token.txt; }; cat ~/token.txt
git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=70 -DCMAKE_BUILD_TYPE=Release
cmake --build build -j --target llama-server
source ~/venv/bin/activate
hf download unsloth/Magistral-Small-2509-GGUF --include "*Q8_0*" --local-dir ~/models/magistral-gguf    # ~25 GB
./build/bin/llama-server -m ~/models/magistral-gguf/*Q8_0*.gguf --jinja -ngl 99 \
  -c 131072 -np 8 --alias magistral-small-1.2 \
  --host 127.0.0.1 --port 8000 --api-key "$(cat ~/token.txt)"
```

- `-c 131072 -np 8` = 8 parallel slots × 16k tokens, split over both GPUs.
- Keep the KV cache in f16: quantised KV (`-ctk q8_0`) has no Volta kernel and silently falls back to the CPU.
- Chat template: Mistral says llama.cpp's built-in template is probably wrong for Magistral and recommends its
  `mistral_common serve` front-end; the Unsloth GGUF ships a corrected template (`--jinja`). If tool calls or
  `[THINK]` handling look wrong in the smoke test, try the `mistral_common` front-end.
- **Q8_0 is 8-bit**: the parity result then includes quantisation. Record it in `OVH_DEPLOY_HARDWARE`.

---

## 6. Connect the harness (on your PC)

A separate PowerShell window holds the tunnel; keep it open for the whole run. It reconnects by itself if the
connection drops (with a passphrase on your key, each reconnect asks for it):

```powershell
$ip = "203.0.113.10"                                       # replace with your instance's public IP
while ($true) { ssh -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -L 8000:127.0.0.1:8000 "ubuntu@$ip"; Write-Host "tunnel closed - reconnecting in 5 s (Ctrl+C stops)"; Start-Sleep 5 }
```

`.env` on your PC (the token is the line `cat ~/token.txt` printed on the instance):

```
OVH_DEPLOY_BASE_URL=http://127.0.0.1:8000/v1
OVH_DEPLOY_TOKEN=<the token>
OVH_DEPLOY_MODEL=magistral-small-1.2
OVH_DEPLOY_HARDWARE=OVH t2-90, 2x V100S 32GB (PCIe), vLLM 0.18.1 fp16 TP=2
```

Keep the PC plugged in with the lid open: `scripts\run_phase6.ps1` keeps Windows from idle sleep while a step runs,
but closing the lid still sends it to sleep — and the instance bills either way.

Then phase 6 of `RUN_PLAN.md`: `probe --models deploy-chat` (dry run first, then `--yes`), the quality suites with
`--session deploy-a`, and one latency session (`--session deploy-lat`). If you enabled Magistral's vendor system
prompt on Bedrock, enable it on `deploy-chat` too (`options.system_prefix_file`) — otherwise the comparison is unequal.

## 7. Read the result

`python analysis/analyze.py` puts `deploy-chat:default` next to `magistral-small-1.2:default` on the same plans.
Read the paired differences:

- **within the confidence interval** → the weights transfer: hosted scores are a fair proxy for this model;
- **outside it** → the serving stack matters (precision, template, tokenizer, parser) — itself a deployment finding.

Volta latency numbers go in the report only as "practice hardware".

**Result of 23 Oct:** within the interval on every suite (RAG correctness 0.710 vs 0.730, CI −0.055 … +0.015); 70 of
100 RAG answers identical word for word because both sides used the same seeds (no leakage — METHODOLOGY §5.8).
Outside it only tool calling (0.84 vs 0.24): the parser, i.e. the host — Bedrock leaves Magistral's calls in the
text. Capacity on this server: comfortable up to 8 RAG requests in flight, usable up to 16, ≈ 51 answers per minute
at most (the report's capacity table).

## 8. Clean up

- Stop the server, **delete the instance** (Public Cloud → Instances → Delete), and check that the project's
  billing shows no running GPU instance.
- Remove `OVH_DEPLOY_TOKEN` from `.env` (or leave it: the endpoint no longer exists).
- No client data ever goes on this machine.

## Sources

- [Magistral Small 2509 model card](https://huggingface.co/mistralai/Magistral-Small-2509) (vLLM command, `mistral_common ≥ 1.8.5`, languages incl. Turkish, sampling)
- [Magistral Small 2509 GGUF](https://huggingface.co/mistralai/Magistral-Small-2509-GGUF) (Q8_0 25.1 GB, template note, `mistral_common serve`)
- [V100 on vLLM 0.20](https://discuss.vllm.ai/t/support-for-v100-sm-70-on-vllm-0-20/2605) (0.20 fails on sm_70; ≤ 0.18.x suggested)
- [PyTorch: Volta dropped from CUDA 12.8 builds in 2.11](https://dev-discuss.pytorch.org/t/dropping-volta-support-from-cuda-12-8-binaries-for-release-2-11/3290) and [vLLM 0.20 moves to PyTorch 2.11](https://newreleases.io/project/pypi/vllm/release/0.20.0)
- Our own check, 23 Oct: the GPU code embedded in `vllm-0.18.1` (PyPI) contains sm_70 SASS, and so does
  `torch-2.10.0+cu128` (PyPI, the build vLLM 0.18.1 pins); vLLM 0.18.1 picks the Triton attention backend on
  compute capability 7.0 (FlashAttention needs 8.0, FlashInfer 7.5)
- [Multimodal encoder profiling hang on V100 in vLLM 0.17](https://github.com/vllm-project/vllm/issues/36357) (image encoder only — text-only serving avoids it)
- [Qwen3.8-27B on a V100 with llama.cpp](https://miyagadget.page/en/blog/2026/09/04/tesla-v100-32gb-qwen38-27b-benchmark-en/) (CUDA 12.x + `-DCMAKE_CUDA_ARCHITECTURES=70`, KV-quant pitfall)
- [1Cat-vLLM](https://github.com/1CatAI/1Cat-vLLM) (V100-focused fork)
