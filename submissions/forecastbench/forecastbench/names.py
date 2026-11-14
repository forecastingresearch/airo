"""ForecastBench's side of the name registry: what each name runs, and keeping it current.

The names themselves are the engine's (fri_forecaster.systems): a system is a
condition run by the top `size` models of a numbered ensemble, and goes out as
<system>-<place>, the place being the ensemble's number in readable form. This
module records, for each name, the settings a ForecastBench round needs to run
it and to report it: its members in fri-utils' ModelRun shape, whether they
research, the AIRO conditions it asks under, and a fingerprint of the code that
shapes its forecasts (system prompt, question prompt, tool descriptions). A
registered entry is never edited.

`sync` keeps the registry in step with the panel, so a round never runs a
stale ensemble and never waits for a hand step. Given the panel (the models
this machine can run, best by ECI first):

  - when the models a system runs changed, the ensemble's number goes up by
    one and EVERY system is registered under the new place, with the panel's
    top `size` and the live code;
  - when only a system's code no longer matches its fingerprint, that system
    is registered as the next revision, <system>-<place>-2.

An entry asks its members under the ForecastBench prompt (`research` says
whether with research) or, when it has `prompts`, under each of those AIRO
conditions (forecastbench.AIRO_CONDITIONS): its forecast is then the mean
over every member under every prompt.

A system is submitted unless it says `"submit": false` (its members still
run, and their forecasts are kept for scoring afterwards), and anonymously
unless it says `"public": true`. ForecastBench takes at most 3 sets a round,
at most one of them public (2026-11-13); `systems` refuses a registry that
breaks either.

The registry is private and lives outside the repo: $FORECASTER_NAMES, else
~/.config/fri-forecaster/names.json. examples/names.example.json shows its shape.

    python -m forecastbench.names systems            # the submitted systems, in set-number order
    python -m forecastbench.names systems --all      # and those that only run
    python -m forecastbench.names public             # the system submitted under the team's name, if any
    python -m forecastbench.names current <system>   # its current name, or exit 1 if the code drifted
    python -m forecastbench.names members <system>   # --member specs for `forecastbench build`
    python -m forecastbench.names sync [--dry-run] < panel   # register what the panel changed
    python -m forecastbench.names show
    python -m forecastbench.names fingerprint <system>   # for registering a name by hand
    python -m forecastbench.names upgrade [--dry-run]    # a registry in the old shape -> this one

The panel on sync's stdin is one model per line, best first:
`<Epoch name> TAB <pi model id> TAB <lab>` (AIRO's `python -m redlines.registry`
prints it). Names alone (`python -m fri_forecaster.eci --fetch --names`) rank
but cannot register a model: sync then keeps the current name and says so.
"""
import copy
import hashlib
import inspect
import json
import sys
from datetime import date

from fri_forecaster import baseline
import forecastbench as fb
from fri_forecaster import harness, panel, systems
from fri_forecaster.systems import parse_panel, registry_path, save  # noqa: F401  (this module's callers use them)
from fri_forecaster.variants import VARIANTS

MAX_SETS = 3     # ForecastBench: at most 3 sets per round,
MAX_PUBLIC = 1   # at most one of them under the team's name (2026-11-13)


def load(path=None):
    return systems.load(path)


def code_of(research, prompts=None):
    """The code-side settings: what the registry entry cannot spell out."""
    if prompts:   # AIRO conditions: the Baseline loop, the question prompt, each instruction
        return {"system": baseline.SYSTEM, "loop": baseline.LOOP,
                "build_prompt": inspect.getsource(fb.build_prompt), "airo_prompt": inspect.getsource(fb.airo_prompt),
                "instructions": {p: VARIANTS[p] for p in prompts},
                "tool_descriptions": {**{t["name"]: t["description"] for t in baseline.research_tools()},
                                      "submit_forecast": fb.TOOL_DESCRIPTIONS["submit_forecast"]}}
    return {"system": fb.SYSTEM if research else fb.SYSTEM_NO_RESEARCH,
            "build_prompt": inspect.getsource(fb.build_prompt),
            "tool_descriptions": fb.TOOL_DESCRIPTIONS if research else {}}


def prompt_version(entry):
    return fb.AIRO_PROMPT_VERSION if entry.get("prompts") else fb.PROMPT_VERSION


def fingerprint(entry):
    """Hash of a name's recorded settings plus the live code they run with."""
    settings = {k: v for k, v in entry.items() if k in ("aggregate", "research", "harness", "members", "prompts")}
    blob = json.dumps({"settings": settings, "code": code_of(entry["research"], entry.get("prompts"))}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def drift(entry):
    """What the live code no longer matches in a registered entry; [] when it runs it."""
    problems = []
    if entry["harness"]["pi"] != harness.PINNED_PI_VERSION:
        problems.append(f"pi {harness.PINNED_PI_VERSION}, registered {entry['harness']['pi']}")
    if entry["harness"]["prompt_version"] != prompt_version(entry):
        problems.append(f"prompt {prompt_version(entry)}, registered {entry['harness']['prompt_version']}")
    if fingerprint(entry) != entry["fingerprint"]:
        problems.append(f"fingerprint {fingerprint(entry)}, registered {entry['fingerprint']}")
    return problems


def current(reg, system):
    """The name to submit under, after checking the code still matches it."""
    name = systems.current(reg, system)
    problems = drift(reg["names"][name])
    if problems:
        sys.exit(f"{name}: the configuration changed ({'; '.join(problems)}). "
                 f"Register it as {systems.name_for(reg, system, systems.ensemble(reg))} in the registry (`sync` does).")
    return name


def member_specs(reg, condition):
    """`forecastbench build --member` specs, MODEL@THINKING,MODE: one per member
    under the ForecastBench prompt, one per member and prompt under AIRO's."""
    entry = reg["names"][current(reg, condition)]
    modes = [f"airo-{p}" for p in entry.get("prompts") or []] or ["research" if entry["research"] else "noresearch"]
    return [f"{m['pi_model']}@{m['options']['thinking']},{mode}" for mode in modes for m in entry["members"]]


def submitted(reg):
    """The systems a round uploads, in set-number order, after checking
    ForecastBench's limits on them."""
    names = [c for c, v in reg["systems"].items() if v.get("submit", True)]
    public = public_of(reg)
    if len(names) > MAX_SETS or len(public) > MAX_PUBLIC:
        sys.exit(f"the registry submits {len(names)} systems ({', '.join(names)}), {len(public)} of them public "
                 f"({', '.join(public) or 'none'}): ForecastBench takes at most {MAX_SETS} sets a round and at most "
                 f"{MAX_PUBLIC} under the team's name. Mark the others \"submit\": false, or not public.")
    return names


def public_of(reg):
    """The submitted systems that go up under the team's name, not its anonymous one."""
    return [c for c, v in reg["systems"].items() if v.get("submit", True) and v.get("public")]


def known_members(reg, research, airo=False):
    """pi model id -> the member record an earlier name with the same research
    setting and the same prompt (ForecastBench's or AIRO's) gave that model (a
    one-shot baseline is a baseline for one setting), preferring a record that
    names its baseline."""
    known = {}
    for e in reg["names"].values():
        if e["research"] != research or bool(e.get("prompts")) != airo:
            continue
        for m in e["members"]:
            if m["pi_model"] not in known or (m.get("one_shot_baseline") and not known[m["pi_model"]].get("one_shot_baseline")):
                known[m["pi_model"]] = m
    return known


def member_for(model, like, known, airo=False):
    """A registry member for a panel model: its fri-utils identity from the
    record an earlier name gave the same pi model when there is one
    (known_members), else from its pi id; the options are `like`'s (one
    condition, one thinking level and tool set for every member), but for the
    thinking level under AIRO's prompts: each model's own top one."""
    old = known.get(model["pi_model"]) or {}
    provider, _, pid = model["pi_model"].partition("/")
    options = copy.deepcopy(like["options"])
    if airo:
        options["thinking"] = panel.reasoning_for(model["pi_model"])
    return {"pi_model": model["pi_model"], "model_key": old.get("model_key", pid), "provider": provider,
            "provider_model_id": old.get("provider_model_id", pid), "lab": old.get("lab") or model["lab"],
            "options": options, "one_shot_baseline": old.get("one_shot_baseline"),
            "eci_name": model["eci_name"]}


def sync(reg, system, panel, today=None):
    """The name a round runs `system` under, given the panel (parse_panel):
    the current one when the ensemble and the code are unchanged; else a new
    name, registered in `reg` with the panel's top `size` and the live code:
    the new ensemble's place when the models any system runs changed, the next
    revision when only this system's code did. -> (name, note). A panel
    without pi ids cannot register a new ensemble: the current one is kept and
    the note warns."""
    sysd = reg["systems"][system]
    name = sysd["current"]
    entry = reg["names"][name]
    n = sysd["size"]
    largest = max(systems.sizes(reg))
    top = panel[:largest]
    have, want = [m["eci_name"] for m in entry["members"]], [m["eci_name"] for m in top[:n]]
    changes, members, warning = drift(entry), entry["members"], None
    ens = systems.ensemble(reg)
    if len(top) == largest and all(m["pi_model"] for m in top):
        ens, _ = systems.advance(reg, [m["pi_model"] for m in top], today)
        if entry.get("ensemble") != ens["version"]:
            changes.insert(0, f"ensemble {entry.get('ensemble', '?')} -> {ens['version']}" +
                           (f": members {', '.join(have)} -> {', '.join(want)}" if set(have) != set(want) else ""))
            airo = bool(entry.get("prompts"))
            members = [member_for(m, entry["members"][0], known_members(reg, entry["research"], airo), airo) for m in top[:n]]
    elif set(have) != set(want):
        warning = (f"WARNING: the top {n} is {', '.join(want) or 'unknown'} but the panel gives no pi ids "
                   f"to register; {name} keeps {', '.join(have)}")
    if not changes:
        return name, warning or f"{name}: ensemble {ens['version']}, the top {n} ({', '.join(have)}), code unchanged"
    new_name = systems.name_for(reg, system, ens)
    new = {"system": system, "ensemble": ens["version"],
           "reason": f"sync {today or date.today().isoformat()}: {'; '.join(changes)}",
           "aggregate": entry["aggregate"], "research": entry["research"],
           **({"prompts": list(entry["prompts"])} if entry.get("prompts") else {}),
           "harness": dict(entry["harness"], pi=harness.PINNED_PI_VERSION, prompt_version=prompt_version(entry)),
           "members": members}
    new["fingerprint"] = fingerprint(new)
    reg["names"][new_name] = new
    sysd["current"] = new_name
    return new_name, f"{new_name}: registered, after {name} ({'; '.join(changes)})" + (f"; {warning}" if warning else "")


def condition_of(entry):
    """The known condition an entry asks under, by name: AIRO's Baseline (and
    its variants), or ForecastBench's own prompt with or without research."""
    if entry.get("prompts"):
        return "+".join("baseline" if p == "none" else p for p in entry["prompts"])
    return "forecastbench" if entry["research"] else "forecastbench-no-research"


def upgrade(old):
    """A registry in the old shape (`conditions`, each numbering its own
    places) -> this one. The ensembles are read off the history of the system
    that ran the most models longest: its places, in order, are the numbers.
    Nothing registered is renamed; a system whose current name is not on the
    current ensemble gets one at the next `sync`."""
    if "systems" in old:
        return old
    reg = copy.deepcopy(old)
    conds = reg.pop("conditions")
    by_system = {}
    for name, e in reg["names"].items():
        by_system.setdefault(e["condition"], []).append(name)
    size = {c: len(reg["names"][v["current"]]["members"]) for c, v in conds.items()}
    lead = max(conds, key=lambda c: (size[c], len(by_system.get(c, []))))
    ensembles = []
    for i, name in enumerate(by_system[lead]):
        place = name.split("-", 1)[1]
        if i >= len(reg["places"]) or reg["places"][i] != place:
            sys.exit(f"cannot read the ensembles off {lead}'s names: {name} is not place {i + 1} ({reg['places'][i:i + 1]})")
        e = reg["names"][name]
        ensembles.append({"version": i + 1, "place": place, "members": [m["pi_model"] for m in e["members"]],
                          **({"registered": e["registered"]} if e.get("registered") else {})})
    for name, e in reg["names"].items():
        e["system"] = e.pop("condition")
        place = name.split("-", 1)[1]
        match = [x for x in ensembles if x["place"] == place
                 and sorted(x["members"][:len(e["members"])]) == sorted(m["pi_model"] for m in e["members"])]
        if match:
            e["ensemble"] = match[0]["version"]
    reg["ensembles"] = ensembles
    reg["systems"] = {c: {**{k: v for k, v in d.items() if k != "current"}, "condition": condition_of(reg["names"][d["current"]]),
                          "size": size[c], "current": d["current"]} for c, d in conds.items()}
    return reg


def main(argv=None, stdin=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd, arg = ([a for a in argv if not a.startswith("--")] + [None, None])[:2]
    if cmd == "upgrade":
        path = registry_path()
        old = json.load(open(path))
        reg = upgrade(old)
        if reg is old:
            print(f"{path}: already in the current shape")
        else:
            ens = systems.ensemble(reg)
            print(f"{path}: {len(reg['ensembles'])} ensembles (now {ens['version']}, {ens['place']}), "
                  f"{len(reg['systems'])} systems, {len(reg['names'])} names" + (" (dry run: not written)" if "--dry-run" in argv else ""))
            if "--dry-run" not in argv:
                save(reg, path)
        return
    reg = load()
    if cmd == "systems":
        print("\n".join(reg["systems"] if "--all" in argv else submitted(reg)))
    elif cmd == "public":
        print("\n".join(public_of(reg)))
    elif cmd == "current" and arg:
        print(current(reg, arg))
    elif cmd == "members" and arg:
        print("\n".join(member_specs(reg, arg)))
    elif cmd == "fingerprint" and arg:
        print(fingerprint(reg["names"][systems.current(reg, arg)]))
    elif cmd == "sync":
        before = json.dumps(reg, sort_keys=True)
        panel = parse_panel(stdin or sys.stdin)
        notes = [sync(reg, c, panel)[1] for c in reg["systems"]]
        changed = json.dumps(reg, sort_keys=True) != before
        if changed and "--dry-run" not in argv:
            save(reg)
        print("\n".join(("(dry run) " if changed and "--dry-run" in argv else "") + n for n in notes))
    elif cmd == "show":
        ens = systems.ensemble(reg)
        print(f"ensemble {ens['version']} ({ens['place']}): {', '.join(ens['members'])}")
        for ch, sysd in reg["systems"].items():
            e = reg["names"][sysd["current"]]
            how = ("not submitted" if not sysd.get("submit", True) else "public" if sysd.get("public") else "anonymous")
            print(f"{sysd['current']:22} {how:13} {sysd['condition']}, top {sysd['size']}: {sysd['description']}"
                  + ("" if e.get("ensemble") == ens["version"] else "  [not on the current ensemble: sync]"))
    else:
        sys.exit("usage: python -m forecastbench.names current|members|fingerprint <system> "
                 "| sync [--dry-run] | systems [--all] | public | show | upgrade [--dry-run]")


if __name__ == "__main__":
    main()
