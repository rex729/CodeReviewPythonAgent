# Build a Free AI Code Review Agent with a Local Model: From Ollama to Automatic GitLab MR Reviews

If you want AI to review your merge requests, the usual options are turning on your platform's built-in AI features or sending your code to a cloud model. But if your code shouldn't leave your machine, or you don't want to pay per review, there's another way: **run an open-source model on your own computer, and write a Python agent that watches for merge requests and reviews them automatically.**

This article builds that agent step by step. By the end, it:

- Watches a list of GitLab projects and reviews every new merge request, plus every new commit pushed to one
- Considers the whole project, not just the diff. For example, it can catch a function whose parameters changed while a caller in another file still uses the old ones
- Posts each review as a thread on the merge request, from a separate bot account
- Runs entirely on your machine, with no cloud AI service and no usage fees, using only Python's standard library

I'll also show what the agent actually caught on a test merge request with seven planted bugs, which problems came from the model and which came from my code, and the guardrails that fixed the latter.

## Quick start

```powershell
ollama pull qwen2.5-coder:7b
# repos.txt: one GitLab repository URL per line (HTTPS or SSH)
# review.config: minutes to wait between rounds, 1-10
# bot_account.config: the bot account's GitLab token with the api scope (gitignored)
python -m review_agent          # keep watching
python -m review_agent --once   # one round, then exit
python -m unittest discover -s tests -t .
./stop_agent.sh                 # stop a running agent (Windows: .\stop_agent.ps1)
```

On macOS, use `python3` (3.11 or later, for `tomllib`).

Reports are written to `workdir/reports/`. Optional settings (model, context size, target branch, file filters) go in `config.toml`; see `config.example.toml`.

---

**PART 1**

## Install Ollama and choose a code review model

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

> **Have more VRAM?** With 24 GB or more, go straight for a larger model like `qwen3-coder:30b`. Its reviews reason much more deeply. You'll see why that matters in Part 6.

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

Once downloaded, the model turns out to be several files: a 4.4 GB weights file, a chat template, a default system prompt, and a license. The **chat template** turns the conversation and tool list into the plain text the model actually reads. It comes up again in Part 2.

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

**PART 2**

## The agent loop: letting the model ask for tools

An agent isn't the model. It's a program that runs the model in a loop and gives it **tools**. The model never executes anything itself. It can only reply with a request like "please run `read_file` on `checkout.py`", and the Python program decides whether to run it.

```
Python ──calls──► model   "Here's a diff. You can use read_file, list_files, search_code."
Python ◄─reply─── model   "Please run read_file with path = checkout.py"   ← just text
Python runs read_file("checkout.py") itself
Python ──calls──► model   "Here's checkout.py: ..."
Python ◄─reply─── model   "- [High] pricing.py:12 — ..."                   ← no request: done
```

The agent offers three tools, all read-only, all pinned to the merge request's version of the code:

- `read_file(path, start_line, end_line)`: a file's contents with line numbers
- `list_files(directory)`: the files under a folder
- `search_code(text)`: lines containing some text, through `git grep`

Read-only is the safety boundary. Whatever the model asks for, it can't change the project.

The core of the loop is short. Tools go in the request's `tools` field as JSON schemas, and each result goes back as a message with the role `tool`:

```python
for _ in range(max_tool_steps):
    reply = llm.chat(messages, tools=TOOL_SCHEMAS)
    messages.append(reply)
    calls = reply.get("tool_calls") or tool_calls_in_text(reply.get("content", ""))
    if not calls:
        return reply["content"]          # no more requests: this is the review
    for call in calls:
        name = call["function"]["name"]
        result = tools.call(name, call["function"]["arguments"])
        messages.append({"role": "tool", "tool_name": name, "content": result})
```

> **Pitfall: qwen2.5-coder writes tool calls as plain text.** Ollama is supposed to put tool requests in the reply's `tool_calls` field. With qwen2.5-coder, my first test came back with an empty `tool_calls` and this in the text instead:
>
> `{"name": "read_file", "arguments": {"path": "src/pricing.py"}}`
>
> The model only writes text in the format its chat template taught it, and Ollama didn't recognize this one as a tool call. Without handling it, the agent would have posted that JSON as a review. The fix is the `tool_calls_in_text` fallback above: if the whole reply is a tool-call JSON for a known tool, treat it as one.

Talking to Ollama doesn't need a library either. The agent sends a plain HTTP POST with `urllib`, so the whole project uses only the standard library.

---

**PART 3**

## Reviewing a merge request with the whole project in mind

Reviewing only the diff misses the bugs that matter most: the ones where a change is fine on its own but breaks something elsewhere. The model's context window can't hold a whole project, though. With `num_ctx` at 16,384 tokens, there's room for roughly 30,000 characters of input. So the agent picks the parts of the project that relate to each change.

First it gets the merge request's real changes. GitLab exposes every MR as a ref, which works even for MRs from forks:

```
git fetch origin +refs/merge-requests/12/head:refs/review/mr/12
git merge-base refs/remotes/origin/main refs/review/mr/12
git diff <merge-base> refs/review/mr/12
```

Then, for each changed file, the model receives:

1. **A project overview.** The model writes it once from the file tree, the README and build files, and the agent caches it until those change.
2. **The merge request itself**: its title, description, and the list of files it changes.
3. **The full new version of the file**, not just the diff.
4. **Definitions of what the diff calls**, found by searching the project for `def`, `fun`, `func`, `class` and C-style `type name(...)` lines.
5. **Other places that use what the diff changes**, which is how it catches a changed signature with stale callers.

Here's the check that convinced me this works. A test merge request changed `apply_discount(price, rate)` from taking 0.1 for 10% off to taking 10, but left a caller in another file passing 0.1. The diff of `pricing.py` alone looks fine. With the usages attached, the model answered in 3 seconds:

```
- [Medium] pricing.py:1 — The signature changed, but the call in checkout.py wasn't updated.
```

It found the problem. It also showed the model's ceiling: this is really a high-severity bug (the discount drops from 10% to 0.1%), and the right fix is changing `0.1` to `10`, not renaming a parameter.

> **Pitfall: C and C++ functions have no keyword.** My first version found definitions by searching for `def`, `fun` or `func`. On a C++ project that found nothing, and the model kept saying "the implementation isn't visible" about functions in the same merge request. C-style definitions are just `int* copy_values(const std::vector<int>& values) {`, so the agent now also matches `type name(` lines, skipping calls like `return name(` and preferring bodies over header declarations.

---

**PART 4**

## Running continuously over many repositories

The agent reads two plain files, both re-read every round, so you can add a repository or change the timing without restarting it.

`repos.txt` lists one repository per line, HTTPS or SSH:

```
# repositories to watch
https://gitlab.com/group/project
git@gitlab.com:group/another-project.git
```

`review.config` holds a single number from 1 to 10: the minutes to wait between rounds.

The wait starts **after a round finishes**, not on a fixed clock. If reviewing five repositories takes five minutes, the next round starts five minutes plus the interval later, so rounds never overlap or pile up:

```
Round 1: repo A → B → C → D → E   (say 5 minutes)
         ↓ all done, read review.config (say 3)
Wait 3 minutes
         ↓
Round 2: re-read repos.txt → A → B → ...
```

Each repository gets its own clone, state and reports, and a failure in one (no access, network down) is logged and skipped so the others still get reviewed.

---

**PART 5**

## Posting reviews as GitLab threads from a bot account

### The account

Even on a public project, **GitLab doesn't allow anonymous comments**, so the agent needs an account and a token. Reading MRs and cloning a public project needs neither.

GitLab's project access tokens, which come with their own bot user, need a Premium plan. On GitLab Free, the alternative is to **register a second, free account for the bot** and create a personal access token for it.

Give the token only the `api` scope, which is what posting comments needs, and an expiry date. Then keep the account's role small. What an API call can do is the token's scopes *and* the account's role in the project, both at once:

```
token scopes  ∩  account's role in the project  =  what the agent can actually do
```

On a public project, the bot doesn't need to be a member at all. Any signed-in account can comment there. For private projects, Reporter is enough. Don't make it a Developer: if the token leaks, a Developer token can push code.

I learned the role half of that rule by accident. I tried to open a merge request with the bot's token and got `403 Forbidden`, although the token had the `api` scope. The bot was a Guest in that project, and opening MRs needs Developer. Commenting, which is all the agent does, works fine as a Guest.

The agent reads the token from `bot_account.config`, or from the `GITLAB_TOKEN` environment variable if that's set. Add that file to `.gitignore` before anything else, so it never gets committed.

### One thread per review

The agent uses GitLab's discussions API, which is what the "Start thread" button does. It creates a resolvable thread instead of a plain comment. Each point the model found becomes one bullet, and the last line names the commit that was reviewed:

```
AI code review (qwen2.5-coder:7b, commit 8d7f3bbc)

- [Medium] helloworld.cpp:11 — the pointer from copy_values is never freed ...
- [Medium] stats.cpp:22 — sprintf can overflow char line[16] ...

##agent reviewed 8d7f3bbcea58
```

That last line is the agent's memory. Before reviewing a merge request, it reads its own latest note there:

- **Same commit as the MR's latest:** skip it without calling the model at all.
- **A different commit, or no thread yet:** delete the agent's old thread, review again, and start a new thread.

Because the record lives on GitLab, a fresh machine or a deleted state file doesn't cause a second review of the same commit, and a thread never mixes findings from different commits. Only the bot's own notes are deleted. Replies people left in the old thread stay.

> **Good to know:** GitLab's discussions endpoint needs authentication even on a public project. Listing open MRs doesn't.

---

**PART 6**

## What it actually caught, and the guardrails it needed

To measure the agent, I opened a merge request on a small C++ project with seven bugs planted in ordinary-looking code: a loop running one past the end, integer division losing the fraction, an `int` sum that can overflow, a `memcpy` that copies `values.size()` bytes instead of `values.size() * sizeof(int)`, a `char line[16]` that `sprintf` can overflow, a missing `delete[]` in the caller, and a `size_t` narrowed to `int`.

The first review posted 13 points. It found 2 of the 7 bugs and half of a third, repeated the memory leak three times, and included confident nonsense such as suggesting `long double` "for precision" and telling the caller to `#include` a header it already included.

Some of that was the model's limits. Some of it was my code. These are the fixes that helped, roughly in order of impact:

1. **Drop points that cite another file.** While reviewing one file, the model also sees related files and reports their bugs too, under the wrong file. Each file's own review covers its own bugs.
2. **Drop points that cite a line outside the changed hunks**, allowing a few lines of slack. This caught invented line numbers, such as line 25 of a header that only has 7 lines.
3. **Give the prompt a checklist of common defect classes**: bounds, integer division and overflow, size calculations such as bytes copied, unreleased resources, empty input. After this, the model started catching integer division and the buffer overflow.
4. **Cap the answer length.** One review ran for over nine minutes because the model got stuck repeating itself until the context filled up. Setting `num_predict` to 1,536 tokens stops that, and when Ollama reports `done_reason: "length"`, the agent drops the half-written last line instead of posting it.
5. **Raise the repeat penalty** to 1.15 over the last 256 tokens. Ollama's default of 1.1 over 64 tokens didn't stop a whole sentence from coming back three times. I kept it mild because reviews quote code, and code legitimately repeats names.

> **Pitfall: don't let a small model delete findings.** I tried using the model to merge reworded duplicates, first by asking which points to keep and then by asking which points repeat each other. Both failed in the worst direction: once it dropped every mention of the out-of-bounds loop, and once it grouped the integer-division bug with an unrelated point, which deleted it. The agent now removes only exact repeats, ignoring line numbers and punctuation. A repeated point costs the reader a few seconds. A deleted bug costs much more.

After the fixes, a typical review of the same merge request finds the out-of-bounds loop, integer division, the `sprintf` overflow and the leak, takes about 30 to 60 seconds, and still includes a wrong suggestion or two. When I fixed one bug and pushed, the agent replaced its thread within one round, and the fixed bug no longer appeared.

The honest summary: **a 7B model makes a useful first pass, not a reviewer you can trust blindly.** It notices real bugs but reasons shallowly. It flagged the faulty `memcpy` line, but for the wrong reason. Results also vary from run to run. Guardrails in code can stop it from posting nonsense in the wrong place, but only a larger model like `qwen3-coder:30b` will make its reasoning deeper.

---

The full agent is about a thousand lines of standard-library Python with 87 unit tests. Start it with `python -m review_agent`, or use `--once` for a single round.
