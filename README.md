# Build a Free AI Code Review Agent with a Local Model: From Ollama to Automatic GitLab MR Reviews

If you want AI to review your merge requests, the usual options are turning on your platform's built-in AI features or sending your code to a cloud model. But if your code shouldn't leave your machine, or you don't want to pay per review, there's another way: **run an open-source model on your own computer, and write a Python agent that watches for merge requests and reviews them automatically.**

This series builds that agent step by step. When it's done, the agent will:

- Watch a GitLab project and review every new merge request, plus every new commit pushed to one
- Consider the whole project, not just the diff. For example, it can catch a function whose parameters changed while a caller in another file still uses the old ones
- Write the results as Markdown reports
- Run entirely on your machine, with no cloud AI service and no usage fees

This first part covers step one: **installing Ollama and choosing a model for code review.**

---

**PART 1**

## Step 1: Install Ollama and choose a code review model

### Model, Ollama, agent: what each one is

Before installing anything, it helps to separate three things that often get blurred together:

- **The model** is a file holding billions of numbers (weights). It doesn't run by itself and has no API. All it can do is read some text and continue writing it.
- **Ollama** is the server that runs models. It stays running in the background and listens on `http://localhost:11434`. When a request comes in, it finds the model the request names, loads it onto the GPU, runs it, and returns the result.
- **The agent** is the Python program we'll write. It decides what to review, which tools the model can use, and what to do with the model's replies.

Here's how they fit together:

```
Python agent
  │ POST http://localhost:11434/api/chat
  │ {"model": "qwen2.5-coder:7b", "messages": [...]}
  ▼
Ollama server (always running in the background)
  │ finds the model file named in "model" → loads it onto the GPU → runs it
  ▼
Model (a weights file on disk)
```

The agent picks the model in the `model` field of every request. Ollama just finds the model by that name and runs it.

### Install Ollama

On Windows, winget installs it in one line:

```powershell
winget install --id Ollama.Ollama --exact
```

You can also download the installer from [ollama.com/download](https://ollama.com/download), which has versions for macOS and Linux too.

Ollama starts in the background once it's installed. Check that the server is running:

```powershell
curl http://localhost:11434/api/version
```

A reply like `{"version":"0.40.13"}` means it worked.

> **That sign-in window isn't required.** After installing, a window may pop up asking you to sign in or connect an ollama.com account. You only need that for Ollama's cloud models. Running models locally needs no account, so you can close the window.

### Where models are stored

The default locations on Windows:

- Ollama itself: `C:\Users\<you>\AppData\Local\Programs\Ollama`
- Downloaded models: `C:\Users\<you>\.ollama\models`

Models take anywhere from a few GB to tens of GB. If your C: drive is short on space, set this environment variable **before downloading any model** to keep models on another drive, then restart Ollama:

```powershell
setx OLLAMA_MODELS "D:\OllamaModels"
```

### Check your hardware before choosing a model

How big a model you can run locally depends almost entirely on your graphics card's memory (VRAM). On an NVIDIA card, check it with:

```powershell
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv
```

Look at `memory.used`. Other programs use VRAM too, so what's left for the model is the total minus what's already in use.

If a model doesn't fit in VRAM, Ollama moves part of it into system RAM and runs that part on the CPU, which is much slower.

### How to choose a model for code review

For a model to act as the brain of a review agent, it needs three things:

1. **Tool calling support.** The agent has to let the model ask for things like "read this file" or "search for this function". Filter by the *Tools* tag on [ollama.com/search](https://ollama.com/search?c=tools) to find models that support it.
2. **Training focused on code.** Models with *coder* or *code* in their names usually understand code better than general models of the same size.
3. **A size that fits your VRAM.** Ollama downloads 4-bit quantized versions by default. They take roughly "parameter count in billions × 0.6 GB", plus extra memory for the context window at runtime.

On my machine (an RTX 5060 with 8 GB of VRAM and 32 GB of RAM), these are the candidates I compared:

#### ✅ qwen2.5-coder:7b (the one I chose)

- 4.7 GB download, up to 32K context
- An open-source code model from Alibaba's Qwen team, and the most commonly recommended choice for 8 GB cards
- Fits entirely on the GPU, so it's fast

#### qwen3-coder:30b

- 19 GB download, up to 256K context
- A mixture-of-experts model with 30B parameters in total but only 3.3B active at a time. It's built for coding agents and gives noticeably better results
- Doesn't fit in 8 GB of VRAM, so most of it runs on the CPU and system RAM. It's still usable, but it takes a lot of RAM

#### rnj-1:8b

- 5.1 GB download, 32K context
- A newer model with impressive official benchmarks and strong tool calling, but less real-world testing from the community so far

> **Have more VRAM?** With 24 GB or more, go straight for a larger model like `qwen3-coder:30b`. Its reviews reason much more deeply.

### Meet qwen2.5-coder

The name `qwen2.5-coder:7b` breaks down into four parts:

- **Qwen**: Alibaba Cloud's family of large language models
- **2.5**: the 2.5 generation
- **Coder**: the code-specialized version, built by taking the general Qwen2.5 model and training it further on large amounts of code
- **7b**: 7 billion parameters

It's an open-weight model, and most of its sizes use the Apache 2.0 license. Once it's downloaded, it runs without an internet connection and costs nothing.

### Download and check the model

```powershell
ollama pull qwen2.5-coder:7b
```

Once downloaded, the model turns out to be several files: a 4.4 GB weights file, a chat template, a default system prompt, and a license. The **chat template** turns the conversation and tool list into the plain text the model actually reads. It will come up again when we write the agent.

Next, talk to the model directly to make sure it works:

```powershell
ollama run qwen2.5-coder:7b "Explain a race condition in one sentence"
```

In another terminal, run `ollama ps` to see how the model is loaded:

```
NAME                SIZE      PROCESSOR    CONTEXT
qwen2.5-coder:7b    5.5 GB    100% GPU     16384
```

The column to watch is `PROCESSOR`. **`100% GPU` means the whole model is on the graphics card**, which is the fastest setup. If you see a split like `40%/60% CPU/GPU`, part of the model is running on the CPU. Try a smaller model or a smaller context window.

Two more things to know:

- **The first call is slow.** My first request took about 55 seconds, mostly to load the model into VRAM. After that, the same request takes a few seconds.
- **Idle models are unloaded.** By default, Ollama moves a model out of VRAM after 5 idle minutes, so the next call has to load it again.

### Test it through the API

The agent talks to Ollama over its HTTP API, so finish with a test through the API (PowerShell):

```powershell
$body = @{
  model = "qwen2.5-coder:7b"
  messages = @(@{ role = "user"; content = "What's wrong with this Python? def avg(xs): return sum(xs) / len(xs)" })
  stream = $false
} | ConvertTo-Json -Depth 5

(Invoke-RestMethod -Uri http://localhost:11434/api/chat -Method Post -Body $body -ContentType "application/json").message.content
```

The model should point out that an empty list causes a division by zero.

---

You now have a code model on your machine that you can call through an API. In the next step, we'll write the core of the agent in Python: giving the model tools to read files in the project, and letting it decide for itself what to look up.
