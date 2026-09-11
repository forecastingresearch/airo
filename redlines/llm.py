#!/usr/bin/env python3
"""The acquisition side's LLM plumbing: key loading + the agentic tool-use loop.

VENDORED on 2026-08-28 from xrisk-canaries' `forecast/cruxgen/_llm.py`, which
`code/run_unified.py` and `code/run_ladder_joint.py` used to import across a
`sys.path` insert into a sibling checkout. That made new elicitation depend on a
second private repo, so nobody outside it could re-run the pipeline that produced
the published forecasts. The loop below is that file's `call_tools` /
`_litellm_complete` / `_fn_spec` carried over unchanged, so a run before and
after the move issues the same calls.

TWO deliberate departures from the original:

  * `load_keys()` is REWRITTEN, not copied. Upstream read Anthropic from the
    xrisk-canaries `.env` and the rest from GCP Secret Manager via a project id
    at a hardcoded macOS path -- machinery that has been a no-op on the cron box
    since day one (see code/cron_run.sh, which sources the keys itself). Copying
    it would have vendored the reproducibility problem instead of fixing it, so
    this version reads an env file, which is what actually happens in production.
  * The Secret Manager path is gone, and with it `python-dotenv` and
    `google-cloud-secret-manager`. Parsing KEY=value needs no dependency. litellm
    is still required and is still imported INSIDE the call, so that importing
    this module -- which tests/ do, to load the runners -- stays stdlib-only and
    the `python3 -m redlines build` path keeps its no-third-party-deps promise.

Nothing here is reachable from `build`. This module is only imported by the
runners under code/, which need `pip install -e '.[acquire]'`.
"""
import json
import os
import sys
from pathlib import Path
import time

from .config import REPO_ROOT

# Every provider key the runners can use. TAVILY is the one that is load-bearing
# rather than optional: run_unified.py refuses to run without it rather than
# quietly publishing forecasts that the provenance line says were web-grounded
# but were not. It serves both grounding tools (web_search and read_page).
# METACULUS_API_KEY left the list on 2026-09-02 with the metaculus_lookup tool
# (redlines/tools.py says why); a value still in the env file is ignored.
PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                 "GOOGLE_API_KEY", "XAI_API_KEY", "TAVILY_API_KEY",
                 # code/observational/run_roster.py: the ECI roster runs
                 # through OpenRouter, one key for all 24 models.
                 "OPENROUTER_API_KEY")

# REDLINES_ENV_FILE is EXCLUSIVE: naming a file means read that file and no
# other, so a run can be pinned to a known key set (and so tests are hermetic on
# a machine that has real keys lying around). With it unset, both defaults are
# read in order and the FIRST value found wins for a given key. A variable
# already in the environment always beats every file, so
# `TAVILY_API_KEY=... python code/run_unified.py` works with no file at all --
# and so does cron_run.sh, which sources the env file before exec'ing us.
DEFAULT_ENV_FILES = ("~/.config/redlines/env",   # the cron box's location (mode 600)
                     "<repo>/.env")              # a local checkout's own file


def _env_paths():
    explicit = os.environ.get("REDLINES_ENV_FILE")
    if explicit:
        return [Path(explicit).expanduser()]
    return [Path.home() / ".config" / "redlines" / "env", REPO_ROOT / ".env"]


def _parse_env(path):
    """KEY=value lines -> dict. Tolerates `export ` prefixes, blank lines, #
    comments, and surrounding quotes -- the same shapes `set -a; . file` accepts,
    since the cron box sources the very same file with the shell."""
    out = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def load_keys():
    """Populate os.environ with provider API keys from the first env file that
    has them. Never overrides a variable that is already set.

    Best-effort by design: a missing file or a missing provider just means that
    provider stays unconfigured, and an Anthropic-only run still works. Callers
    that actually need a specific key check for it themselves after calling this
    (run_unified.py refuses to start without TAVILY_API_KEY). Returns the sorted
    names of the keys that ARE configured afterwards, for logging.
    """
    for path in _env_paths():
        values = _parse_env(path)
        for k in PROVIDER_KEYS:
            if values.get(k):
                os.environ.setdefault(k, values[k])
    return sorted(k for k in PROVIDER_KEYS if os.environ.get(k))


def _fn_spec(tool):
    """A {name, description, parameters} tool -> the OpenAI/litellm function spec."""
    return {"type": "function", "function": {
        "name": tool["name"], "description": tool.get("description", ""),
        "parameters": tool["parameters"]}}


# Per-request timeout, seconds. litellm's default is 600, which the combined
# instrument (2,520 probabilities in one tool call) overran on GPT-5.5 Pro in
# the 2026-08-28 pilot: "Connection timed out after 600.0 seconds", a lost
# call. A slow model on a long instrument needs longer; set it per run.
def request_timeout():
    return float(os.environ.get("REDLINES_LLM_TIMEOUT", "600"))


# Reasoning level, per provider family. Project lead, 2026-09-02: reasoning
# should be at its maximum. litellm's `reasoning_effort` is the one knob across
# providers; the top rung is spelled differently per family, and a rung a
# model does not offer comes back as a 400, so the map is by model prefix and
# `_litellm_complete` steps down one rung on that error before giving up on
# reasoning altogether. Probed 2026-09-02 on the box, tools attached:
# Fable 5 / Opus 5 take "max"; GPT-5.5 Pro and GPT-5.6 Sol take "xhigh"
# (Sol rejects tools ONLY under litellm's default effort -- an explicit level
# is fine). Anthropic's effort also governs how many tool calls the model
# makes, which is the point. REDLINES_REASONING overrides for an experiment
# ("none" sends no reasoning parameter at all).
REASONING = {"anthropic": "max", "openai": "xhigh"}
REASONING_DEFAULT = "high"
# The rungs the panel is configured for. A model configured at one of these
# is never run without reasoning: if every rung is rejected the call fails.
TOP_RUNGS = {"max", "xhigh"}
# The oldest litellm known to map "max" to Anthropic's adaptive thinking and
# "xhigh" to OpenAI's reasoning.effort (verified on the cron box, 2026-09-10).
LITELLM_MIN = "1.96"
REASONING_STEP_DOWN = {"max": "high", "xhigh": "high", "high": "medium", "medium": "low",
                       "low": "minimal"}


def reasoning_for(model):
    """The reasoning_effort to send for `model`, or None for none."""
    override = os.environ.get("REDLINES_REASONING")
    if override:
        return None if override.lower() == "none" else override
    return REASONING.get(model.split("/", 1)[0], REASONING_DEFAULT)


# An EXPLICIT Anthropic thinking budget, tokens per turn -- off by default.
# Probed on the box 2026-09-02 evening (AnthropicConfig.map_openai_params):
# for the Claude 5 family litellm treats reasoning_effort="max" as ADAPTIVE
# thinking with output_config.effort="max" -- Anthropic's own top setting,
# the model deciding how much to think per turn. The fixed
# budget_tokens=16,384 alias exists only for models without adaptive
# thinking. So "max" IS max here, and an explicit budget would be a
# downgrade (a fixed cap in place of adaptive effort). The knob stays for
# experiments: REDLINES_THINKING_BUDGET=<tokens> sends
# thinking={enabled, budget_tokens} instead, kept THINKING_HEADROOM under
# max_tokens (Anthropic requires max_tokens > budget_tokens).
THINKING_BUDGET = {}
THINKING_HEADROOM = 16000


def thinking_budget_for(model, max_tokens):
    """The `thinking.budget_tokens` to send for `model` under `max_tokens`, or
    None (no explicit budget: reasoning_effort, or nothing, applies)."""
    if reasoning_for(model) is None:
        return None
    override = os.environ.get("REDLINES_THINKING_BUDGET")
    budget = int(override) if override else THINKING_BUDGET.get(model.split("/", 1)[0])
    if not budget:
        return None
    return max(1024, min(budget, max_tokens - THINKING_HEADROOM))


# {model: (effort, temperature, budget)} that the provider accepted, per
# process, so the step-down ladder runs once per model rather than per turn.
_ACCEPTED = {}


def _litellm_complete(model, messages, *, tools=None, tool_choice=None,
                      max_tokens=8000, temperature=0.0):
    """One litellm completion, normalized to {content, tool_calls:[{id,name,arguments}],
    thinking_blocks?}. `thinking_blocks` is Anthropic's signed reasoning; the
    loop puts it back on the assistant turn, which the API requires when a
    tool result follows a thinking turn."""
    import litellm
    litellm.drop_params = True
    litellm.suppress_debug_info = True
    kw = dict(model=model, messages=messages, max_tokens=max_tokens, num_retries=2,
              timeout=request_timeout())
    if tools:
        kw["tools"] = tools
    if tool_choice:
        kw["tool_choice"] = tool_choice
    effort = reasoning_for(model)
    budget = thinking_budget_for(model, max_tokens)

    def attempt(effort, temp, budget=None):
        extra = {}
        if budget:
            extra["thinking"] = {"type": "enabled", "budget_tokens": budget}
        elif effort:
            if model.startswith("openai/responses/"):
                # litellm maps reasoning_effort only for models in its own
                # model map and DROPS it silently for the rest (drop_params):
                # GPT-6 Astra's requests went out with no effort at all
                # (verified 2026-09-10 by capturing the request body). The
                # Responses API's native field passes through for any model.
                extra["reasoning"] = {"effort": effort}
            else:
                extra["reasoning_effort"] = effort
                if model.startswith("openai/"):
                    # Same drop on the chat route for an unmapped model; this
                    # forces the parameter through (litellm's allow-list).
                    extra["allowed_openai_params"] = ["reasoning_effort"]
        if temp is not None:
            extra["temperature"] = temp
        return litellm.completion(**kw, **extra)

    # Two error families carry a rejected request parameter: the provider's
    # 400 (litellm.BadRequestError: "`temperature` is deprecated", "thinking
    # .type.enabled is not supported") and litellm's own mapping failure before
    # any request is made (litellm.APIConnectionError, "Unmapped reasoning
    # effort: max" on a litellm build that predates the rung). A model can
    # reject several in a row -- Opus 4.8 on litellm 1.7x: temperature, then
    # "max", then every fixed-budget rung -- so this walks a ladder: drop the
    # temperature, drop an explicit thinking budget, step the effort down one
    # rung at a time to none (the model's own default), and only then give up.
    # Each step is logged, so the run log says what effort a call actually ran.
    rejected = (litellm.BadRequestError, litellm.APIConnectionError)
    temp = temperature
    # Start from what this model accepted earlier in the process: a model
    # that rejects `temperature` (the OpenAI reasoning models) would otherwise
    # pay one refused request per turn of every call.
    known = _ACCEPTED.get(model)
    if known:
        effort, temp, budget = known
    for _ in range(12):
        before = (effort, temp, budget)
        try:
            r = attempt(effort, temp, budget)
            _ACCEPTED[model] = (effort, temp, budget)
            break
        except rejected as e:
            msg = str(e).lower()
            # litellm itself could not map the rung: the installed litellm
            # predates it (1.81 has no "max"; the cron box's 1.96 maps it to
            # Anthropic's adaptive thinking). That is a setup fault, not a
            # model limit -- refuse rather than run a top-rung model at a
            # lower effort without anyone noticing.
            if "unmapped reasoning effort" in msg:
                raise RuntimeError(
                    f"{model}: litellm cannot map reasoning_effort={effort!r} "
                    f"({str(e)[:80]}); install litellm>={LITELLM_MIN} (pip install -e '.[acquire]')") from e
            if temp is not None and "temperature" in msg:
                temp = None
            elif budget and ("thinking" in msg or "budget" in msg):
                budget = None
            elif effort and ("reasoning" in msg or "effort" in msg or "thinking" in msg):
                lower = REASONING_STEP_DOWN.get(effort)
                if lower is None and effort in TOP_RUNGS:
                    # The provider rejected every rung of a model configured
                    # for the top one. Running it with no reasoning at all
                    # would be a different experiment; stop instead.
                    raise RuntimeError(
                        f"{model}: every reasoning rung was rejected ({str(e)[:100]}); "
                        f"not falling back to no reasoning for a model configured "
                        f"for {reasoning_for(model)!r}. Check the litellm version "
                        f"(>={LITELLM_MIN}) and the model id.") from e
                effort = lower
            else:
                raise
            if (effort, temp, budget) == before:
                raise
            print(f"  note  {model}: request rejected ({str(e)[:90]}...); retrying with "
                  f"effort={effort or 'none'} temperature={temp} budget={budget}",
                  file=sys.stderr)
    else:
        raise RuntimeError(f"{model}: no accepted request configuration after stepping down")
    requested = reasoning_for(model)
    if effort != requested:
        print(f"WARN: {model} ran with reasoning_effort={effort or 'none'}, not the configured "
              f"{requested or 'none'} (see the notes above); the row's usage records both",
              file=sys.stderr)
    msg = r.choices[0].message
    calls = [{"id": tc.id, "name": tc.function.name,
              "arguments": tc.function.arguments or "{}"}
             for tc in (getattr(msg, "tool_calls", None) or [])]
    out = {"content": msg.content, "tool_calls": calls, "usage": _usage_of(r),
           # What this turn actually ran with, so a row can show it (tally_usage).
           "reasoning_effort": effort, "reasoning_requested": requested}
    # OpenAI reports reasoning tokens separately; a reasoning-effort request
    # that comes back with none on a substantial turn is the parameter having
    # been dropped somewhere (a one-word reply may legitimately use none).
    if effort and model.startswith("openai") and out["usage"] is not None \
            and out["usage"].get("output_tokens", 0) >= 500 \
            and not out["usage"].get("reasoning_tokens"):
        print(f"WARN: {model} was asked for reasoning_effort={effort} but reported no "
              "reasoning tokens; check that the effort reached the provider", file=sys.stderr)
    blocks = getattr(msg, "thinking_blocks", None)
    if blocks:
        out["thinking_blocks"] = blocks
    return out


def _usage_of(r):
    """One completion's tokens and price: {input_tokens, output_tokens,
    reasoning_tokens?, cached_tokens?, cost_usd} -- cost_usd is None when
    litellm has no price for the model, so an unpriced call is visible rather
    than free. None when the response carries no usage at all."""
    import litellm
    u = getattr(r, "usage", None)
    if u is None:
        return None
    out = {"input_tokens": getattr(u, "prompt_tokens", 0) or 0,
           "output_tokens": getattr(u, "completion_tokens", 0) or 0}
    ctd = getattr(u, "completion_tokens_details", None)
    rt = getattr(ctd, "reasoning_tokens", None) if ctd is not None else None
    if rt:
        out["reasoning_tokens"] = rt
    ptd = getattr(u, "prompt_tokens_details", None)
    ct = getattr(ptd, "cached_tokens", None) if ptd is not None else None
    if ct:
        out["cached_tokens"] = ct
    try:
        out["cost_usd"] = float(litellm.completion_cost(completion_response=r))
    except Exception:   # litellm raises its own family for an unknown model
        out["cost_usd"] = None
    return out


def tally_usage(total, one, effective=None):
    """Add one completion's usage into the running total for a call (in
    place): turns, tokens by kind, cost_usd, and unpriced_turns -- the turns
    litellm could not price, so a total with any of those is a floor.
    `effective` (the completion's {reasoning_effort, reasoning_requested})
    stamps the effort the call actually ran with, so a row records it."""
    if total is None:
        return
    total["turns"] = total.get("turns", 0) + 1
    if effective is not None:
        total["reasoning_effort"] = effective.get("reasoning_effort")
        total["reasoning_requested"] = effective.get("reasoning_requested")
    if not one or one.get("cost_usd") is None:
        total["unpriced_turns"] = total.get("unpriced_turns", 0) + 1
    else:
        total["cost_usd"] = round(total.get("cost_usd", 0.0) + one["cost_usd"], 6)
    for k in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens"):
        if one and one.get(k):
            total[k] = total.get(k, 0) + one[k]


def call_tools(model, prompt, tools, final_tool, *, max_iters=6, system=None,
               max_tokens=8000, temperature=0.0, complete=None, min_evidence=0,
               usage=None, partial_tool=None):
    """Agentic loop: let the model call `tools` to gather evidence, then submit its
    answer via `final_tool`. Returns (answer_args, evidence).

    `partial_tool` (2026-09-02 evening): a tool the model may call any number
    of times to deliver its answer IN PIECES -- offered exactly when
    `final_tool` is, served by its `fn` (which keeps what it is given; the
    caller merges after the loop), never counted as research evidence. The
    final tool's call then carries the rest. This keeps a large structured
    answer clear of the per-turn output cap: the combined instrument's 2,520
    probabilities in one tool call were most of a 64k-token turn, shared with
    the thinking budget; in pieces, no turn is large, and the budget can be.

    `tools` are dicts {name, description, parameters, fn}; `final_tool` is the same
    minus fn (its args ARE the structured answer -- no regex parsing). On the last
    iteration the loop forces `final_tool` so the model can't stall. `evidence` is
    the ordered list of {tool, args, result} from the non-final tool calls.

    `min_evidence` is the RESEARCH FLOOR (2026-09-02): until that many tool
    calls have returned, `final_tool` is not offered at all -- the model can
    only research -- and a premature call to it is answered with how many
    calls remain. The floor is what makes "iterative multi-step search" a
    property of the harness rather than a hope about the prompt: asked
    nicely, Fable 5 searched four times and read nothing (the 2026-09-02
    smoke). The last iteration still forces submission, floor or no floor.

    `usage`, if given, is a dict the loop fills in place with the call's
    tokens and price across every turn (tally_usage) -- pass the same dict
    through a retry and it keeps counting, so a call's cost includes the
    attempts that failed.

    `complete` is the completion backend (defaults to litellm); tests inject a fake
    one to drive the loop offline. Raises if the model never submits.
    """
    complete = complete or _litellm_complete
    by_name = {t["name"]: t for t in tools}
    research_specs = [_fn_spec(t) for t in tools]
    final_spec = _fn_spec(final_tool)
    partial_spec = _fn_spec(partial_tool) if partial_tool else None
    final_choice = {"type": "function", "function": {"name": final_tool["name"]}}
    messages = ([{"role": "system", "content": system}] if system else [])
    messages.append({"role": "user", "content": prompt})
    evidence = []
    trace_dir = os.environ.get("REDLINES_TRACE_DIR")
    trace_path = None
    if trace_dir:
        directory = Path(trace_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        trace_path = directory / f"{model.replace('/', '_')}-{time.time_ns()}.json"

    def checkpoint(iteration, status, response=None, error=None):
        if trace_path is None:
            return
        state = {"model": model, "prompt": prompt, "system": system,
                 "iteration": iteration, "status": status, "messages": messages,
                 "evidence": evidence, "usage": usage, "response": response,
                 "error": error, "max_iters": max_iters, "max_tokens": max_tokens}
        temporary = trace_path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream)
        temporary.replace(trace_path)

    cannot_force = model.startswith(("anthropic/claude-fable-5-1", "anthropic/claude-mythos"))
    # Unsupported endpoints get two additional submission-only turns. These
    # retain the same conversation and accepted cells, never restart research.
    limit = max_iters + (2 if cannot_force else 0)

    for i in range(limit):
        force = i >= max_iters - 1
        offered = force or len(evidence) >= min_evidence
        specs = research_specs + (([partial_spec] if partial_spec else []) + [final_spec] if offered else [])
        # Fable 5.1 rejects forced tool_choice at the API boundary. Keep its
        # reasoning setting and accumulated research, offer just the final
        # tool, and request submission explicitly on the termination turn.
        # https://github.com/anthropics/skills/blob/main/skills/claude-api/shared/tool-use-concepts.md
        auto_submit = force and cannot_force
        if auto_submit:
            specs = [final_spec]
            messages.append({"role": "user", "content": (
                f"Now call {final_tool['name']} with your final answer. "
                "Include any remaining forecast cells and required summary fields; "
                "previously accepted cells are retained.")})
            if usage is not None:
                usage["submission_guard"] = "auto-final-tool-only"
        checkpoint(i, 'request')
        try:
            resp = complete(model, messages, tools=specs,
                            tool_choice=(final_choice if force and not auto_submit else "auto"),
                            max_tokens=max_tokens, temperature=temperature)
        except Exception as exc:
            checkpoint(i, 'failed', error=str(exc))
            raise
        tally_usage(usage, resp.get("usage"),
                    {k: resp.get(k) for k in ("reasoning_effort", "reasoning_requested")}
                    if "reasoning_effort" in resp else None)
        checkpoint(i, 'response', response=resp)
        calls = resp.get("tool_calls") or []
        if not calls:
            # answered in prose without submitting -- nudge it to use the tool
            asst = {"role": "assistant", "content": resp.get("content") or ""}
            if resp.get("thinking_blocks"):
                asst["thinking_blocks"] = resp["thinking_blocks"]
            messages.append(asst)
            messages.append({"role": "user", "content": (
                f"Now call {final_tool['name']} with your final answer." if offered else
                f"Keep researching with the tools: {final_tool['name']} is offered after "
                f"{min_evidence} tool calls have returned ({len(evidence)} so far).")})
            continue
        # the assistant turn must carry tool_calls for the tool replies to validate
        asst = {"role": "assistant", "content": resp.get("content") or "",
                "tool_calls": [{"id": c["id"], "type": "function",
                                "function": {"name": c["name"],
                                             "arguments": c["arguments"]}}
                               for c in calls]}
        if resp.get("thinking_blocks"):
            asst["thinking_blocks"] = resp["thinking_blocks"]
        messages.append(asst)
        for c in calls:
            try:
                args = json.loads(c["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if c["name"] == final_tool["name"]:
                if offered:
                    checkpoint(i, 'complete', response=resp)
                    return args, evidence
                result = {"error": f"{final_tool['name']} is not offered yet: "
                                   f"{min_evidence - len(evidence)} more research call(s) "
                                   f"must return first ({len(evidence)} of {min_evidence} so far)"}
            elif partial_tool and c["name"] == partial_tool["name"]:
                # A piece of the answer: kept by the caller's fn, not evidence.
                result = (partial_tool["fn"](**args) if offered else
                          {"error": f"{partial_tool['name']} is not offered yet: "
                                    f"{min_evidence - len(evidence)} more research call(s) "
                                    f"must return first ({len(evidence)} of {min_evidence} so far)"})
            else:
                spec = by_name.get(c["name"])
                result = spec["fn"](**args) if spec else {"error": f"unknown tool {c['name']}"}
                evidence.append({"tool": c["name"], "args": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": json.dumps(result)[:8000]})
    checkpoint(limit, 'failed', error='submission limit reached')
    raise RuntimeError(f"{final_tool['name']} not called within {limit} iterations; "
                       f"recorded usage={usage}; trace={trace_path}")
