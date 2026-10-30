# Model lineup — what is compared, why, and where it runs

Target hardware for client deployments: **one or two RTX PRO 6000 Blackwell (96 GB) or H200 (141 GB)** cards.
So the lineup centres on models that fit those cards, measured as **the same weights** on hosted APIs (Track H),
plus EVREN's fleet (Track A, free until 2026-11-01) and one self-deployed model on OVHcloud V100S (Track B).

Facts come from model cards and provider pages, checked on 2026-10-10 (sources at the end). Memory figures are
weights only; "fits" means weights + ~3 GB runtime per GPU leave **≥ 20 % of VRAM for the KV cache**
(enough for about 8–16 concurrent 32k-token streams on a 27B model).

> **Public version.** This file describes the whole October 2026 lineup. The results of EVREN's `auto` router
> and of the three models on NVIDIA's API trial (Muse Glimmer 30B, Nemotron 3.5 Lightning, Nemotron 3.5 Super VL)
> are not published: `auto` is a router, not a model, and NVIDIA's trial terms allow internal evaluation only.

---

## 1. Core chat lineup

| Model | Role (config `tier`) | Where we test it | Size, architecture | Fits (1×96 / 2×96 / 1×141 / 2×141 GB) | Licence | Turkish claimed? | Thinking control |
|---|---|---|---|---|---|---|---|
| **Qwen3.8-27B** | deployable, 1 GPU (`deployable-1gpu`) | OVHcloud AI Endpoints | 27B dense (hybrid attention) | BF16 on all four (56 GB) | Apache-2.0 | not stated (Qwen3 listed Turkish) | `enable_thinking`; `reasoning_effort` xhigh (default) / medium / low |
| **Gemma 4 31B** | deployable, 1 GPU (`evren-1gpu-class`) | EVREN (free); Ollama optional | 31B dense | BF16 on all four (61 GB) | Apache-2.0 | "140+ languages", Turkish not named | `<\|think\|>` / `enable_thinking`; an empty thought block when off |
| **Qwen3-VL-30B-A3B** | deployable, 1 GPU (`evren-1gpu-class`) | EVREN (free) | 31B MoE, 3B active | BF16 on all four (62 GB) | Apache-2.0 | not named | separate instruct / thinking checkpoints |
| **gpt-oss-120b** | deployable, 1 GPU (`deployable-1gpu`) | OVHcloud | 117B MoE, 5.1B active | MXFP4 on all four (65 GB) | Apache-2.0 + usage policy | **no** — English-centric control | `reasoning_effort` low / medium / high; cannot be switched off |
| **Mistral Small 4** (Mistral, EU) — **not in the run** | deployable, 1 GPU at 4-bit (config: `disabled`) | none: NVIDIA's free endpoint answered HTTP 410 on 10 Oct ("end of life on 2026-07-27"); only Mistral's own API is left (optional O0, needs a key) | 119B MoE, 6.5B active (128 experts, 4 active) | NVFP4 on 1×96 (71 GB; Blackwell MoE kernels still young) and on 1×141 (H200 has no FP4 tensor cores: speed unverified); FP8 on 2×96 / 2×141 (121 GB) | Apache-2.0 | **yes, listed** (`tr` among 24 languages) | `reasoning_effort` none / high |
| **Muse Glimmer 30B** (Meta) | deployable, 1 GPU (`deployable-1gpu`) | NVIDIA's free endpoint | 29.6B dense incl. a 1.8B vision encoder | BF16 on all four (≈60 GB) | Apache-2.0 | "100+ languages", Turkish not named | cannot be switched off: strength low / medium / high (default) / xhigh |
| **Nemotron 3.5 Lightning 30B-A3B** (NVIDIA) | deployable, 1 GPU (`deployable-1gpu`) | NVIDIA's free endpoint | 30B MoE, ~3B active | BF16 on all four (≈60 GB) | NVIDIA Open Model License | not listed — a control | `enable_thinking` (works); a reasoning budget did not shorten its reasoning on NVIDIA's endpoint (11 Oct) |
| **Magistral Small 1.2** (Mistral, EU) | practice twin (`deployable-1gpu`) | Amazon Bedrock **and** our OVH deployment | 24B dense | BF16 on all four (48 GB) | Apache-2.0 | **yes, listed** | reasons in `[THINK]…[/THINK]` only under Mistral's reasoning system prompt, with those markers as special tokens; **on Bedrock it did not reason — not even with that prompt sent as plain text (11 Oct) — and wrote tool calls as text** (10 Oct). Self-hosted (phase 6, 23 Oct): same quality, tool calls returned as calls (0.84 vs 0.24 on Bedrock); its think mode was not run |
| **Nemotron 3 Super** | deployable, 2 GPUs (`deployable-2gpu`) | Amazon Bedrock | 120B hybrid MoE, 12B active | FP8 on 2×96 / 2×141 (128 GB); NVFP4 on 1×96 is tight | NVIDIA Open Model License | **no** (7 languages) | `enable_thinking`, `low_effort`, reasoning budget; **on Bedrock: no reasoning, and the probe found no switch** (10 Oct) |
| **Nemotron 3.5 Super VL 120B-A12B** (NVIDIA, 15 Oct 2026) — added 16 Oct | deployable, 1 GPU at 4-bit (`deployable-1gpu`) | NVIDIA's free endpoint (phase 4b) | 120B hybrid MoE (Mamba-2 / Transformer), 12B active, C-RADIO vision encoder (text side tested) | NVFP4 (NVIDIA's quantisation-aware checkpoint, weights ≈68 GB): 1×141 per NVIDIA, 1×96 tight; BF16 needs 4×H100 | OpenMDW-1.1 | **no** (Indian languages only) — a control | `enable_thinking` via `chat_template_kwargs` (NVIDIA's example); budget switch: the probe tries the generic ones |
| **gpt-oss-20b** | small baseline (`deployable-1gpu`) | OVHcloud | 21B MoE, 3.6B active | any card (13 GB) | Apache-2.0 + usage policy | no | as gpt-oss-120b |
| **Qwen3.5-9B** | small baseline (`deployable-1gpu`) | OVHcloud | 9B dense | any card (19 GB) | Apache-2.0 | "201 languages", Turkish not named | `enable_thinking` (on by default) |
| **Qwen3.8-Flash-Next** | 2×H200 class (`evren-2gpu-class`) | EVREN (free) | 180B stored (125B MoE, 6B active + n-gram table) | FP8 on 2×141 (32 % free); 2×96 only with host-RAM offload | Qwen Community License 1.0 — **check before client use** (selling model access / "AI work assistant" products need a separate licence) | not stated | `enable_thinking`, `reasoning_effort` |
| **DeepSeek-V4-Flash** | 2×H200 class (`evren-2gpu-class`) | EVREN (free; **removed 1 Nov**) | 284B MoE, 13B active | 2×141 (41 % free; FP4 runs on slower kernels on H200); 2×96 marginal | MIT | not stated | adjustable thinking budget on EVREN (the probe finds the parameter) |
| **Qwen3.5-397B-A17B** | ceiling — an upper bound, not a recommendation (`ceiling`) | OVHcloud | 397B MoE, 17B active | only INT4 on 2×141 (14 % free) | Apache-2.0 | not named | `enable_thinking` |

EVREN's other chat models — **GLM 5.3** (753B), **DeepSeek V4.1 Flash** (763B), **MiMo V2.6 Pro** (1T) and the
`auto` router — run in the EVREN battery because they are free, but they need multi-GPU servers and are shown as
*ceilings / service measurements*, never as on-prem recommendations.

**Why these.** The 1-GPU block covers the realistic choices for a single 96–141 GB card: a dense 27–31B model
(Qwen3.8-27B, Gemma 4 31B, Muse Glimmer 30B), a small-active MoE (Qwen3-VL-30B-A3B, gpt-oss-120b, Nemotron 3.5
Lightning, and since 16 Oct Nemotron 3.5 Super VL at 4-bit) and the EU option that names Turkish (Magistral Small 1.2, hosted on Bedrock and self-hosted in phase 6;
Mistral Small 4 would have been the second, see below). gpt-oss and Nemotron are deliberate **controls**: none of them claims Turkish,
so they show what "no Turkish training" costs. The two small baselines show
how far a 9–21B model gets on each task type — often enough for routing, guards or extraction.

**Hosted proxies are proxies.** OVHcloud and NVIDIA do not disclose quantisation; Bedrock and Ollama serve their own
builds. Phase 6 (Magistral self-deployed vs Bedrock) measured this gap once, on 23 Oct: none on any suite — the
same weights in fp16 on 2× V100S scored like Bedrock within noise, 70 of 100 RAG answers word for word the same
(same seeds). The one difference was the host: Bedrock returns Magistral's tool calls as text, our vLLM server as
tool calls (METHODOLOGY §5.8).

**NVIDIA's API Catalog** (build.nvidia.com) hosts several current open models on free, rate-limited trial endpoints
(~40 requests/min per key; terms: internal testing and evaluation only). It is how this plan reaches Muse Glimmer
30B, Nemotron 3.5 Lightning and (since 16 Oct) Nemotron 3.5 Super VL, which none of the paid providers serve. Such endpoints can end without notice: Mistral
Small 4's did (HTTP 410 on 10 Oct, end of life 27 Jul 2026).

### Reasoning modes per model (probes of 10–11 Oct, checked on 5 RAG questions on 11 Oct)

What each system's thinking can be set to on *its host*, as measured — the RAG battery runs every mode marked yes
(METHODOLOGY.md §1.1). "–" = does not apply (the default does not think, or no budget switch was configured).

| Model (host) | Default thinks? | nothink | think_min | think |
|---|---|---|---|---|
| Gemma 4 31B (EVREN) | yes | yes — `enable_thinking: false` | – | – |
| Qwen3-VL-30B (EVREN) | no | – | – | no switch |
| Qwen3.8-Flash-Next (EVREN) | yes | yes — `enable_thinking: false` | no: `reasoning_effort` did not cut it enough | – |
| DeepSeek V4 Flash, V4.1 Flash (EVREN) | yes | yes — `enable_thinking: false` | no: no switch cut it enough | – |
| GLM-5.3 (EVREN) | yes | **yes since 19 Oct** — `"thinking": false`, EVREN's documented switch (15 Oct): 0 reasoning, Turkish answers (probe and 5-question smoke, 19 Oct); until then **no**: `enable_thinking: false` wrote the reasoning into the answer (smoke, 11 Oct; Turkish share 1.00 → 0.07) | yes — `reasoning_effort: low` (80 / 62 reasoning tokens vs 234 / 261; 60 / 64 on 19 Oct); on the RAG questions it did not reason at all (acts as nothink there) | – |
| MiMo V2.6 Pro (EVREN) | yes | yes — `enable_thinking: false` | no: `minimal` / `low` did not cut it enough | – |
| Qwen3.8-27B, Qwen3.5-9B, Qwen3.5-397B (OVHcloud) | yes | yes — `reasoning_effort: none` | no | – |
| gpt-oss-120b, gpt-oss-20b (OVHcloud) | yes | no (cannot be switched off) | yes — `reasoning_effort: low` | – |
| Magistral Small 1.2 (Bedrock) | no | – | – | **no**: Mistral's reasoning prompt sent as plain text had no effect (step 0, 11 Oct); it needs `[THINK]` as special tokens — phase 6 |
| Nemotron 3 Super (Bedrock) | no | – | – | no switch found |
| Muse Glimmer 30B (NVIDIA) | yes | no (cannot be switched off) | yes — `reasoning_strength: low` | – |
| Nemotron 3.5 Lightning (NVIDIA) | yes | yes — `enable_thinking: false` | **no**: `{"reasoning": {"budget": 1}}` passed the old probe rule but reasoned more than the default in the RAG smoke | – |
| Nemotron 3.5 Super VL (NVIDIA) | yes | yes — `enable_thinking: false` (0 reasoning tokens, answers stay Turkish; probe 16 Oct) | **no**: none of 7 budget switches cut its ≈500 reasoning tokens (`thinking_budget` and a numeric `reasoning_effort` refused) | – |

In the RAG battery that makes 15 EVREN systems (8 default, 6 nothink — GLM-5.3's since 19 Oct, phase 3f — GLM-5.3 think_min) and 16 hosted ones
(OVHcloud 10, Bedrock 2, NVIDIA 4), plus Nemotron 3.5 Super VL's two (default and nothink; phase 4b, 16 Oct). One of Nemotron 3.5 Lightning's five default
smoke answers was degenerate text — noted, one sample.

### Mistral: Small 4 is the current model, but out of reach; Magistral Small 1.2 represents Mistral

**Mistral Small 4** (16 Mar 2026) is Mistral's current small model and has absorbed Magistral: its reasoning mode
is "previously Magistral", and Mistral's own API has deprecated Magistral. It is Apache-2.0, lists Turkish and fits
1×96 GB at NVFP4 or two cards at FP8 — the Mistral a client would deploy. OVHcloud lists no Mistral model (its
hidden ones retire on 2026-11-04), Bedrock offers Large 3, Magistral Small 2509, Ministral 3 and Devstral 2, and
Ollama Cloud only Large 3/4. Without a Mistral API key the plan was to use **NVIDIA's free endpoint**
(`mistralai/mistral-small-4-119b-2603`), which build.nvidia.com still listed as *Deprecated* — but on 10 Oct it
answered HTTP 410 ("reached its end of life on 2026-07-27"). Mistral's own EU API (optional O0, needs a key) is now
the only hosted route. NVIDIA also ships the model as a
downloadable **NIM** container — free to self-host for development on up to 16 GPUs under the NVIDIA Developer
Program, production use needs NVIDIA AI Enterprise — but, like vLLM (0.20 or newer is required), it targets
FP8/FP4-capable GPUs (FP8 on 2× H200 / B200, NVFP4 on one B200), not our V100S; it is a deployment path for a
client's RTX PRO 6000 / H200 servers. llama.cpp support is still in progress. So the practice deployment keeps
**Magistral Small 1.2** (24B, fp16 on 2×32 GB), whose Bedrock twin gives the self-hosted vs hosted comparison. Mistral Medium 3.5 (128B dense, Apr 2026) is stronger but
its modified MIT licence gives no rights to companies above US$20M monthly revenue. Mistral Large 4 (preview since
6 Oct, ~1T parameters) is an optional ceiling.

### Is the lineup current? (checked 2026-10-10)

- **Qwen.** Qwen3.8-27B (14 Aug) is the newest dense Qwen; Qwen3.8-Flash-Next (26 Aug, on EVREN) the newest MoE that
  fits 2×H200. Nothing newer exists: Alibaba said on 22 Sep that Qwen4 "is currently in training", with no date. The
  newest Qwen MoEs for one or two cards — **Qwen3.6-35B-A3B** and **Qwen3.5-122B-A10B** — are not served by any of
  our providers (EVREN's Qwen3-VL-30B-A3B is the older stand-in); test them on client hardware. Qwen3.8-2.4T needs
  multiple nodes and has its own licence. No Qwen card names Turkish (Qwen3.5 claims 201 languages).
- **Mistral.** Small 4 is the current model, but its free NVIDIA endpoint ended before our run (above), so Magistral
  Small 1.2 represents Mistral; Large 4 preview optional; Ministral (3) and Devstral (2) have no newer versions.
- **Meta.** **Muse Spark** (1.0 in April, 1.1 July, 1.2 on 5 Aug, 1.3 on 2 Sep) is proprietary and API-only, size
  undisclosed; open weights for 1.2 were pledged in August and have not appeared. So "the big one" cannot be
  deployed on-prem today. The only open Muse model is **Muse Glimmer** (10 Aug): ≈29.6B dense including a 1.8B vision
  encoder, Apache-2.0, 131K context, reasoning effort low–xhigh, Turkish not named. It fits one card in BF16
  (≈60 GB) and is **in the plan through NVIDIA's free endpoint**. Meta and Unsloth also publish GGUF builds for
  llama.cpp (build b10353 or newer), so it could additionally be self-hosted on the phase-6 OVH machine (8-bit,
  ≈34 GB, on 2× V100S) for a self-hosted vs hosted pair; reasoning cannot be switched off (levels low–xhigh,
  default high). Llama has had no new open weights since Llama 4.
- **Google.** Gemma 4 (31B dense, 26B-A4B MoE; 12B added in June) is current; there is no Gemma 4.x or 5. Gemma 4
  26B-A4B and 31B are also on Bedrock's OpenAI-compatible "mantle" endpoint (optional O2b).
- **Others.** gpt-oss / gpt-oss-safeguard are still OpenAI's newest open weights; NVIDIA added Nemotron 3 Ultra
  (550B, too big), a compressed Nemotron Puzzle-75B and **Nemotron 3.5 Lightning 30B-A3B** (in the plan via NVIDIA's
  endpoint; Turkish not listed), and on 15 Oct **Nemotron 3.5 Super VL 120B-A12B** (open weights, OpenMDW-1.1; in the
  plan since 16 Oct via NVIDIA's endpoint, phase 4b; Turkish not listed); DeepSeek's newest, V4.1-Flash, is 763B.

---

## 2. Optional systems (run only when named; see `configs/campaign_optional.yaml`)

| Model | Where | Why it is optional |
|---|---|---|
| Gemma 4 31B on Ollama Cloud | Ollama (free plan: 1 request in flight) | host-parity check against EVREN's Gemma 4 31B |
| Nemotron 3 Nano 30B-A3B | Ollama | one more 1-GPU MoE; Turkish not listed |
| Qwen3.6-27B | OVHcloud | previous Qwen generation — "is the new one better in Turkish?" |
| Mistral Large 3 (675B) | Bedrock | EU ceiling; not deployable on 1–2 GPUs |
| Mistral Large 4 (preview, ~1T) | Ollama Cloud | Mistral's newest ceiling; weights announced for about 31 Oct, licence not announced; preview outputs can change, so rerun before publishing |
| Gemma 4 26B-A4B and 31B | Bedrock (mantle endpoint) | Gemma's small-active MoE (fits one card) and a third host for the 31B; needs the `bedrock-mantle:CreateInference` permission |
| Qwen3-Embedding-8B on OVHcloud | OVHcloud | embedding host-parity check against EVREN |

---

## 3. Pipeline components

| Task | Systems | Notes |
|---|---|---|
| **Embeddings** | qwen3-embedding-8b (EVREN), bge-multilingual-gemma2 (OVHcloud), bge-m3, multilingual-e5-large-instruct, turkish-e5-large, embeddinggemma-300m (local), Cohere embed-multilingual-v3, Titan v2 (Bedrock, managed references) + BM25 with Turkish stemming; **9 small local embedders** (next table) | public Turkish leaderboards disagree (TR-MTEB: turkish-e5-large 66.0, mE5-large-instruct 65.9, embeddinggemma 65.2; Mizan: embeddinggemma 65.4, bge-m3 62.9; TR-TEB (LREC 2026) puts embeddinggemma-300m at 48.5) — hence our own retrieval test on Turkish policy and law text |
| **Rerankers** | qwen3-reranker-8b (EVREN), bge-reranker-v2-m3 and **modernbert-tr-reranker** (local) | OVHcloud has no reranker; Bedrock's Cohere Rerank needs SigV4 or InvokeModel and is documented for English |
| **Guards** | Qwen3Guard-Gen 0.6B / 4B / 8B (EVREN, OVHcloud — free), gpt-oss-safeguard 20B / 120B (Bedrock), YTU COSMOS modernbert-tr-guardrail ≈150M (this PC; added 30 Oct) | none of the five hosted ones names Turkish as an evaluated language; COSMOS's model is trained on Turkish. Qwen3Guard uses its own template; gpt-oss-safeguard follows **our Turkish policy** (`prompts/tr/guard_policy.md`). Any chat model can be tested as a guard with the same policy |
| **OCR** | dots-ocr, deepseek-ocr-2 (EVREN), **Unlimited-OCR** (Baidu, 3B, MIT — local GPU, added 14 Oct), Tesseract 5 `tur` (local) | Unlimited-OCR: DeepSeek-OCR-style document parser, model card ParseBench mean 46.17, no languages named; runs in its own environment (transformers 4.57.1) on the measuring laptop's RTX 4060 — its seconds per page are this laptop's, not a server's |
| **Speech-to-text** | qwen3-asr-1.7b (EVREN), Whisper-large-v3 and -turbo (OVHcloud, per second), Whisper-large-v3 (local, faster-whisper) | |
| **Decision** | laya and **clef** (EVREN `/v1/systemone`; clef added 14 Oct) vs every chat model with the same label guide | clef: Cloudflare's typed-decision model [Clef](https://huggingface.co/Cloudflare/clef) (Apache-2.0), post-trained from Qwen3.8-27B; on EVREN 16K context, up to 4 images, no batch endpoint, no `answer_confidence` (EVREN docs) |

All of EVREN's pipeline models are open weights (Apache-2.0 / MIT) that fit one GPU: qwen3-embedding-8b,
qwen3-reranker-8b (Qwen3-VL-Reranker-8B per EVREN's catalogue), qwen3-guard-4b, dots-ocr (dots.mocr, 3B),
deepseek-ocr-2 (3.4B), qwen3-asr-1.7b and the two decision models, laya (0.3B) and clef (Cloudflare's Clef, 27B,
post-trained from Qwen3.8-27B). Unlike EVREN's large chat models, their EVREN quality scores therefore carry over to
an on-prem deployment of the same weights. One caveat: per its catalogue EVREN serves laya as `evren/laya-tr-v5`, a
Turkish fine-tune of mmBERT-base, while the published Laya models are multilingual; we found no public copy of the
Turkish one (checked 30 Oct).

### Small embedders that run on a laptop or a home GPU (phase 3c, all local and free)

Same 90 questions and 195 chunks as the API embedders, so they land in one table. Each also runs with the
Turkish ModernBERT reranker on top — a fully local pipeline under 1B parameters.

| Model | Size | Turkish | Licence | On Ollama | Published scores | How we run it |
|---|---|---|---|---|---|---|
| EmbeddingGemma 2 (Google, 6 Oct 2026) | 744M checkpoint; 270M text path | not named (100+ languages) | Apache-2.0 | yes | MMTEB 61.4; no Turkish score yet | prompts `SearchQuery` / `Document`; bf16 or fp32 (fp16 gives NaN) |
| embeddingmagibu2-text-v1 (Magibu AI, 12 Oct 2026) — added 13 Oct | 271M (EmbeddingGemma 2's text tower) | Turkish model (42k Turkish subwords in the tokenizer) | Apache-2.0, gated (instant approval) | no | developer's 10-task Turkish retrieval: 0.582 nDCG@10 (teacher 0.571; embeddinggemma-300m 0.590; multilingual-e5-large-instruct 0.587); not an official MTEB run | prompts `query` / `document`; bf16 or fp32; pinned revision |
| nomic-embed-text-v2-moe | 475M MoE, 305M active | yes (`tr` tag) | Apache-2.0 | yes | MIRACL 65.8; Mizan 59.5 | `search_query: ` / `search_document: `; inputs cut at 512 tokens |
| nomic-embed-text v1.5 — **control** | 137M | English only | Apache-2.0 | yes (`nomic-embed-text`) | Mizan 40.3 | same prefixes; shows what a popular English embedder does on Turkish |
| UltraBERT-Embed-150M (DMIS Lab, Oct 2026) — **control 2**, added 14 Oct | 151M (own architecture: 24 layers, sliding-window + global attention, 32k context) | English (text and code) only | Apache-2.0 | no | MTEB(eng, v2) 67.24 — the best ~150M English embedder in its own table; MTEB(Code) 73.3; no multilingual or Turkish score | its `query` prompt, documents plain; SDPA attention, bf16; pinned revision — does a fresh English record transfer to Turkish? |
| Harrier OSS 270M / 0.6B (Microsoft, Apr 2026) | 0.27B / 0.6B | yes | MIT | no | MMTEB 66.5 / 69.0 | its `web_search_query` instruction for queries, plain documents; bf16 or fp32 |
| Granite Embedding 311M multilingual r2 (IBM, Apr 2026) | 311M (ModernBERT) | yes | Apache-2.0 | no (Ollama cannot run ModernBERT) | MMTEB retrieval 65.2 | no prefixes |
| Qwen3-Embedding-0.6B | 0.6B | not named (100+ languages) | Apache-2.0 | yes | MMTEB 64.3; TR-TEB 59.1 (retrieval 50.8) | the same instruction as EVREN's 8B |
| modernbert-tr-embed (YTU COSMOS, 2026) | ≈150M | Turkish model | Apache-2.0 | no | distilled from Qwen3-Embedding-8B | its `query` prompt, plain documents |
| multilingual-e5-large | 560M | yes | MIT | – | Mizan 61.5; TR-TEB retrieval 58.6 | `query: ` / `passage: ` |
| modernbert-tr-reranker (YTU COSMOS, 2026) — **reranker** | ≈150M cross-encoder | Turkish model | Apache-2.0 | no | distilled from Qwen3-Reranker-8B | 512 tokens, fp32 (as its card recommends) |
| modernbert-tr-guardrail (YTU COSMOS, 2026) — **guard**, added 30 Oct | ≈150M classifier | Turkish model (≈405k Turkish prompts) | Apache-2.0 | no | its card: unsafe F1 0.930 on its own Turkish test split | prompt as-is, fp32; unsafe when p(unsafe) > 0.5, fixed before the run |

bge-m3 (phase 3, also on Ollama) has the best published Turkish retrieval score of the small ones (TR-TEB
retrieval 59.3, level with Qwen3-Embedding-8B's 59.6). Left out: jina-embeddings v5 (CC BY-NC), Snowflake Arctic
Embed v2 and KaLM mini v2.5 (weaker published Turkish scores).

**Guards left out:** Llama Guard 4 (no Turkish; Llama licence), Nemotron Safety Guard (Turkish not named; rights end if
guardrails are bypassed), Granite Guardian (English only), ShieldGemma 2 (English, images), Shieldstral 1.0 (12
languages, not Turkish), Mistral moderation (hosted API only). **Added on 30 Oct:** YTU COSMOS
`modernbert-tr-guardrail` (Apache-2.0, ≈150M, trained on Turkish) — a classifier rather than a chat model, so it
runs in-process on this PC (`guard_format: classifier`, phase 7); a prompt counts as unsafe when its unsafe
probability is above 0.5, a rule fixed before the run.

---

## 4. The judge: Claude Opus 5.5

| Option | Price per 1M tokens (in / out), Batch API | Judging repeat 0 of the core lineup (≤ 3,900 answers: 39 systems after the probe) | Use |
|---|---|---|---|
| **Claude Opus 5.5** | $2 / $10 (sync $4 / $20) | ≤ $51 | **the judge** |
| Claude Sonnet 5.5 | $1 / $5 | ≤ $25 | calibration on a fixed 4 % sample (≈ $1.0); the judge for future engagements **if** its agreement with the human labels is as good |
| Claude Fable 5.1 | $5 / $25 | ≤ $127 | not needed for routine judging; at most a tie-breaker on the few answers where Opus and the human labels disagree |
| Claude Haiku 5.5 | $0.05 / $0.25 (prompts ≤ 100K tokens) | – | not a judge; the cheap "do these two repeats say the same?" screener of the adaptive repeat judging (≈$1 for all repeats) |

All four support Anthropic's Message Batches API (half price; most batches finish within an hour, at most 24 h),
which the harness uses by default (`configs/judge.yaml: transport: batch`).

**Why Opus.** Judging is reading Turkish context, splitting an answer into claims and checking each against the
passages — careful reading, not frontier reasoning, so Fable's extra capability is unlikely to change enough
verdicts to justify 2.5× the cost. Opus is the safer default than Sonnet for a published benchmark; the
calibration step tells us whether Sonnet is enough next time. No Claude model is among the systems under test, so
there is no self-preference. Estimates use ~3 characters per token; Turkish may tokenise less efficiently on
Claude, so allow +30 %.

**Why not Qwen3.5-397B (OVHcloud) as a judge, even for part of the answers.** It is a system under test (the
ceiling row) and shares the Qwen family with four other systems, so family preference cannot be ruled out; and
mixing judges inside one table makes verdicts incomparable. At most it could grade the 4 % calibration sample as a
cross-family check on Opus.

---

## 5. Considered and not included

| Model | Why not |
|---|---|
| Qwen3.5-122B-A10B, Qwen3.6-35B-A3B | good 2-GPU / 1-GPU Apache candidates, but not served by our providers → Track B on client hardware |
| Meta Muse Spark | proprietary and API-only, size undisclosed; open weights promised but not released |
| MiniMax M3.1 Flash (released 19 Oct; free on Ollama Cloud until 22 Oct, US-hosted) | closed and API-only — weights promised for later, size and licence undisclosed; thinking cannot be switched off (default effort max); no Turkish claim; considered 20 Oct, same rule as Muse Spark. Its open sibling MiniMax M3 (428B / 23B active) would be the candidate — licence and providers not checked |
| On NVIDIA's free endpoints but left out | GLM-5.3-Flash (320B / 18B active: 2 GPUs only at 4-bit; Chinese / English), Kimi K3 (~2.8T), Nemotron 3 Ultra (550B), Laguna XS 2.1 (coding), DiffusionGemma 26B-A4B (diffusion LLM, niche), nemotron-3-embed-1b (Turkish not among its 34 evaluated languages), Llama Guard 4 and the Nemotron safety guards (Turkish not claimed; would need output adapters) |
| Qwen3.8-2.4T-A95B | multi-node; its own licence |
| Cohere Command A+ | the only large Apache model that names Turkish, but 218B (2×141 at W4A4 only) and not on our providers |
| EuroLLM-22B, Turkish-Gemma-9b-T1, Trendyol-LLM-8B-T1 | Turkish-capable small models with no hosted API; easy extra self-hosted tests (fp16 fits the V100S machine) |
| Mistral Medium 3.5, Devstral 2 | licence: no rights for companies above US$20M monthly revenue |
| Aya Expanse, Tiny Aya, jina-embeddings v3/v4/v5 | non-commercial (or research-only) licences |
| MiniMax-M2.7 | commercial use needs written permission |
| Nemotron 3 Ultra, Llama 4 Scout | too big for 1–2 cards / Turkish not supported |

---

## 6. The practice deployment (Track B): why OVHcloud t2-90 and Magistral Small 1.2

Your OVHcloud credits cover only Volta GPUs: **t1** (V100 16 GB ×1/2/4) and **t2** (V100S 32 GB ×1/2/4). Volta has
**no bf16, no FP8 and no FlashAttention-2**; current vLLM releases and CUDA 13 have dropped it. So the practice run
proves the deployment path and quality parity, **not** the speed of RTX PRO 6000 / H200.

- **t2-90 (2× V100S 32 GB)** holds Magistral Small 1.2 in fp16 (48 GB) with tensor parallelism 2 and room for a
  16k-token KV cache. t2-45 (1 card) would need 8-bit weights; t2-180 is spare capacity.
- Magistral is also on Bedrock, so the same plans give a **self-hosted vs hosted** paired comparison.
- Step-by-step: `deploy/OVH_V100S_RUNBOOK.md`.

---

## Sources (checked 2026-10-10)

- Model cards: [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B), [Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B),
  [Qwen3.5-397B-A17B](https://huggingface.co/Qwen/Qwen3.5-397B-A17B), [Qwen3.8-Flash-Next](https://huggingface.co/Qwen/Qwen3.8-Flash-Next)
  ([licence](https://huggingface.co/Qwen/Qwen3.8-Flash-Next/blob/main/LICENSE)), [Qwen3-VL-30B-A3B](https://huggingface.co/Qwen/Qwen3-VL-30B-A3B-Instruct),
  [Gemma 4 31B](https://huggingface.co/google/gemma-4-31B-it), [gpt-oss-120b](https://huggingface.co/openai/gpt-oss-120b),
  [gpt-oss model card](https://arxiv.org/pdf/2508.10925), [gpt-oss-safeguard guide](https://developers.openai.com/cookbook/articles/gpt-oss-safeguard-guide),
  [Magistral Small 2509](https://huggingface.co/mistralai/Magistral-Small-2509), [Mistral Small 4](https://huggingface.co/mistralai/Mistral-Small-4-119B-2603),
  [Mistral Medium 3.5 licence](https://huggingface.co/mistralai/Mistral-Medium-3.5-128B/blob/main/LICENSE),
  [Nemotron 3 Super](https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b/modelcard), [Nemotron 3 Nano](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16),
  [DeepSeek-V4-Flash](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash), [Command A+](https://huggingface.co/CohereLabs/command-a-plus-05-2026-w4a4),
  [EuroLLM-22B](https://huggingface.co/utter-project/EuroLLM-22B), [Turkish-Gemma-9b-T1](https://huggingface.co/ytu-ce-cosmos/Turkish-Gemma-9b-T1),
  [Qwen3Guard-Gen-8B](https://huggingface.co/Qwen/Qwen3Guard-Gen-8B), [Qwen3Guard report](https://arxiv.org/pdf/2510.14276).
- Added on 2026-10-30 for the links in the README, each opened that day: [GLM-5.3](https://huggingface.co/zai-org/GLM-5.3);
  MiMo-V2.6-Pro in two checkpoints, [RL](https://huggingface.co/XiaomiMiMo/MiMo-V2.6-Pro-RL) (21 Sep) and
  [MOPD](https://huggingface.co/XiaomiMiMo/MiMo-V2.6-Pro-MOPD) (27 Sep) — EVREN serves `xiaomi/mimo-v2.6-pro` and does not
  say which; [DeepSeek-V4.1-Flash](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash); [gpt-oss-20b](https://huggingface.co/openai/gpt-oss-20b);
  [Nemotron 3 Super weights](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16).
  EVREN's pipeline models as its catalogue (9 Oct) names them: [Qwen3-Embedding-8B](https://huggingface.co/Qwen/Qwen3-Embedding-8B),
  [Qwen3-VL-Reranker-8B](https://huggingface.co/Qwen/Qwen3-VL-Reranker-8B) (`qwen3-reranker-8b`),
  [Qwen3Guard-Gen-4B](https://huggingface.co/Qwen/Qwen3Guard-Gen-4B) (`qwen3-guard-4b`), [dots.mocr](https://huggingface.co/dots-studio/dots.mocr)
  (`dots-ocr`), [DeepSeek-OCR-2](https://huggingface.co/deepseek-ai/DeepSeek-OCR-2), [Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B);
  laya is `evren/laya-tr-v5` per the catalogue, a Turkish fine-tune of [mmBERT-base](https://huggingface.co/jhu-clsp/mmBERT-base) (no
  public copy found); the published [Laya](https://huggingface.co/convaiinnovations/laya) models are multilingual and use the same
  question types (choice, score, noul). clef is Cloudflare's [Clef](https://huggingface.co/Cloudflare/clef) (Apache-2.0, post-trained
  from Qwen3.8-27B; its card says the API is compatible with Jev and SystemOne). Other pipeline models: [Qwen3Guard-Gen-0.6B](https://huggingface.co/Qwen/Qwen3Guard-Gen-0.6B),
  [gpt-oss-safeguard-20b](https://huggingface.co/openai/gpt-oss-safeguard-20b), [gpt-oss-safeguard-120b](https://huggingface.co/openai/gpt-oss-safeguard-120b),
  [Whisper large-v3](https://huggingface.co/openai/whisper-large-v3), [Whisper large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo),
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (its [large-v3 conversion](https://huggingface.co/Systran/faster-whisper-large-v3)),
  [bge-m3](https://huggingface.co/BAAI/bge-m3), [multilingual-e5-large](https://huggingface.co/intfloat/multilingual-e5-large),
  [multilingual-e5-large-instruct](https://huggingface.co/intfloat/multilingual-e5-large-instruct),
  [turkish-e5-large](https://huggingface.co/ytu-ce-cosmos/turkish-e5-large), [embeddinggemma-300m](https://huggingface.co/google/embeddinggemma-300m),
  [bge-multilingual-gemma2](https://huggingface.co/BAAI/bge-multilingual-gemma2), [bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3);
  on Bedrock, [Cohere Embed Multilingual v3](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-cohere-embed-multilingual.html)
  and [Titan Text Embeddings V2](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-titan-text-embeddings-v2.html).
  Platforms: [EVREN](https://evren.ssyz.org.tr/), [Amazon Bedrock](https://aws.amazon.com/bedrock/), [vLLM](https://github.com/vllm-project/vllm).
- Providers: [OVHcloud AI Endpoints catalog](https://www.ovhcloud.com/en/public-cloud/ai-endpoints/catalog/),
  [OVHcloud billing / retirements](https://docs.ovhcloud.com/en/guides/public-cloud/ai-machine-learning/ai-endpoints-billing),
  [Bedrock Chat Completions](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html),
  [Bedrock API compatibility](https://docs.aws.amazon.com/bedrock/latest/userguide/models-api-compatibility.html),
  [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/), [Ollama pricing](https://ollama.com/pricing),
  [Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility.md), [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing).
- Mistral: [Mistral Small 4 announcement](https://mistral.ai/news/mistral-small-4/), [models and deprecations](https://docs.mistral.ai/models),
  [changelog](https://docs.mistral.ai/resources/changelogs), [Mistral Large 4](https://huggingface.co/mistralai/Mistral-Large-4-1T-A52B),
  [Mistral Small 4 price (third-party)](https://www.cloudzero.com/blog/mistral-api-pricing).
- Qwen: [Qwen releases](https://huggingface.co/models?author=Qwen&sort=created), [Alibaba roadmap, 22 Sep](https://www.media-outreach.com/news/singapore/2026/09/22/489176/alibaba-unveils-roadmap-on-full-stack-ai-strategy-from-chips-cloud-infrastructure-models-to-agents/).
- Nemotron 3.5 Super VL (15 Oct 2026): [NVIDIA technical blog](https://developer.nvidia.com/blog/nvidia-nemotron-3-5-super-vl-powers-specialized-agents-with-multimodal-reasoning/),
  [weights, NVFP4](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4) (BF16 and base checkpoints alongside),
  [release summary (MarkTechPost)](https://www.marktechpost.com/2026/10/15/nvidia-nemotron-3-5-super-vl-release/?amp=1),
  [launch notes with caveats (OrcaRouter)](https://www.orcarouter.ai/blog/nemotron-3-5-super-vl-launch).
- NVIDIA API Catalog: [Muse Glimmer 30B](https://build.nvidia.com/meta/muse-glimmer-30b), [Nemotron 3.5 Lightning](https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b),
  [Mistral Small 4](https://build.nvidia.com/mistralai/mistral-small-4-119b-2603) ([API reference](https://docs.api.nvidia.com/nim/reference/mistralai-mistral-small-4-119b-2603),
  [NIM container](https://catalog.ngc.nvidia.com/orgs/nim/mistralai/containers/mistral-small-4-119b-2603),
  [NIM for VLMs: GPUs and licences](https://docs.nvidia.com/nim/vision-language-models/latest/introduction.html)),
  [vLLM recipe for Mistral Small 4](https://recipes.vllm.ai/mistralai/Mistral-Small-4-119B-2603),
  [API Trial Terms of Service](https://assets.ngc.nvidia.com/products/api-catalog/legal/NVIDIA%20API%20Trial%20Terms%20of%20Service.pdf),
  [observed ~40 requests/min limit](https://news.creeta.com/en/nvidia-nim-free-tier-limits-2026/),
  [nemotron-3-embed-1b](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-embed-1b).
- Meta: [Muse Spark](https://en.wikipedia.org/wiki/Muse_Spark), [Muse Glimmer 30B](https://huggingface.co/meta-models/Muse-Glimmer-30B),
  [Glimmer GGUF](https://huggingface.co/meta-models/Muse-Glimmer-30B-GGUF),
  [VentureBeat on Glimmer](https://venturebeat.com/technology/meta-returns-to-open-source-with-muse-glimmer-an-apache-2-0-licensed-30b-parameter-ai-model-optimized-for-agents-available-now),
  [The Register on Spark's open weights](https://www.theregister.com/ai-and-ml/2026/09/02/zucks-muse-to-spark-joy-with-open-weights-release-soon/5294093),
  [Glimmer on SageMaker JumpStart](https://aws.amazon.com/about-aws/whats-new/2026/01/muse-glimmer-30b-qwen-3.8-27b-on-sagemaker-jumpstart/).
- Google: [Gemma 4 12B](https://blog.google/innovation-and-ai/technology/developers-tools/introducing-gemma-4-12b/),
  [Gemma 4 26B-A4B on Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-google-gemma-4-26b-a4b.md).
- Small embedders: [embeddingmagibu2-text-v1](https://huggingface.co/alibayram/embeddingmagibu2-text-v1) (and the
  multimodal [embeddingmagibu2-v1](https://huggingface.co/alibayram/embeddingmagibu2-v1), same text weights),
  [EmbeddingGemma 2](https://blog.google/innovation-and-ai/technology/developers-tools/embeddinggemma-2)
  ([model card](https://huggingface.co/google/embeddinggemma-2)), [nomic-embed-text-v2-moe](https://huggingface.co/nomic-ai/nomic-embed-text-v2-moe),
  [nomic-embed-text v1.5](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5),
  [UltraBERT-Embed-150M](https://huggingface.co/dmis-lab/UltraBERT-Embed-150M) (commit `367d534`), [Harrier 270M](https://huggingface.co/microsoft/harrier-oss-v1-270m),
  [Harrier 0.6B](https://huggingface.co/microsoft/harrier-oss-v1-0.6b), [Granite Embedding r2](https://huggingface.co/ibm-granite/granite-embedding-311m-multilingual-r2),
  [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B), [modernbert-tr-embed](https://huggingface.co/ytu-ce-cosmos/modernbert-tr-embed),
  [modernbert-tr-reranker](https://huggingface.co/ytu-ce-cosmos/modernbert-tr-reranker), [modernbert-tr-guardrail](https://huggingface.co/ytu-ce-cosmos/modernbert-tr-guardrail),
  Ollama pages for [embeddinggemma-2](https://ollama.com/library/embeddinggemma-2), [nomic-embed-text-v2-moe](https://ollama.com/library/nomic-embed-text-v2-moe)
  and [nomic-embed-text](https://ollama.com/library/nomic-embed-text).
- OCR: [Unlimited-OCR](https://huggingface.co/baidu/Unlimited-OCR) (pinned commit `07dea83`; the vLLM recipe at
  recipes.vllm.ai is Linux-only, the harness uses the model card's transformers path).
- Turkish embedding leaderboards: [TR-MTEB](https://aclanthology.org/2025.findings-emnlp.471.pdf), [TR-TEB (LREC 2026)](https://aclanthology.org/2026.lrec-1.862.pdf), [Mizan](https://newmindai-mizan.hf.space/).
- Judge: [Message Batches API](https://platform.claude.com/docs/en/build-with-claude/batch-processing), [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing),
  [Claude models](https://platform.claude.com/docs/en/models/overview).
- Hardware: [RTX PRO 6000 Blackwell Server](https://www.nvidia.com/en-us/data-center/rtx-pro-6000-blackwell-server-edition/),
  [H200](https://www.nvidia.com/en-eu/data-center/h200/), [V100 on vLLM 0.20](https://discuss.vllm.ai/t/support-for-v100-sm-70-on-vllm-0-20/2605),
  [Qwen3.8-27B on a V100 with llama.cpp](https://miyagadget.page/en/blog/2026/09/04/tesla-v100-32gb-qwen38-27b-benchmark-en/).
