"""Systems: the names forecasts go out under, the same in every project.

A system is a condition (how the panel is asked: baseline, ...) run by the top
`size` models of an ensemble. Ensembles are numbered. When the models a system
runs change, the number goes up by one and every system moves with it, so all
of an installation's systems always carry the same number.

A name is <system>-<place>. The place is only the readable form of the
ensemble's number: the registry's list of places, read off by position. A
system whose code changes while the ensemble stays gets a revision,
<system>-<place>-2: a name never covers two methods.

The registry is private (it ties leaderboard names to a team) and lives outside
the repository: $FORECASTER_NAMES, else ~/.config/fri-forecaster/names.json.

    {"places": ["oak", "elm", ...],
     "ensembles": [{"version": 1, "place": "oak", "members": [pi ids, best first], "registered": "YYYY-MM-DD"}],
     "systems": {"<system>": {"description": "...", "condition": "baseline", "size": 4,
                              "current": "<system>-oak", "public": true, "submit": true}},
     "names": {"<system>-oak": {...the settings it was registered with...}}}

This module reads the registry and numbers ensembles. Registering a name's
settings is the project's that needs them (forecastbench.names).

    python -m fri_forecaster.systems show               # each system's current name
    python -m fri_forecaster.systems current <system>   # one name
"""
import json
import os
import sys
from datetime import date


def registry_path():
    return os.environ.get("FORECASTER_NAMES") or os.path.expanduser("~/.config/fri-forecaster/names.json")


def load(path=None):
    path = path or registry_path()
    if not os.path.exists(path):
        sys.exit(f"no name registry at {path}: set FORECASTER_NAMES")
    reg = json.load(open(path))
    if "systems" not in reg:
        sys.exit(f"{path} is in the old shape (conditions, no ensembles): run `python -m forecastbench.names upgrade`")
    return reg


def save(reg, path=None):
    path = path or registry_path()
    with open(path + ".tmp", "w") as f:
        json.dump(reg, f, indent=1)
        f.write("\n")
    os.replace(path + ".tmp", path)


def parse_panel(lines):
    """A panel, best first: `<Epoch name> TAB <pi model id> TAB <lab>` per line
    (AIRO's `python -m redlines.registry` prints it). A name alone ranks but
    cannot be registered."""
    panel = []
    for line in lines:
        f = [p.strip() for p in line.rstrip("\n").split("\t")] + [None, None]
        if f[0]:
            panel.append({"eci_name": f[0], "pi_model": f[1] or None, "lab": f[2] or None})
    return panel


def sizes(reg):
    return sorted({s["size"] for s in reg["systems"].values()})


def ensemble(reg):
    """The current ensemble: the newest one."""
    return reg["ensembles"][-1]


def _signature(members, by_size):
    """What makes two ensembles the same: for each system size, which models
    are its top that many. An order change that moves no model across a size
    is not a new ensemble."""
    return [sorted(members[:n]) for n in by_size]


def advance(reg, top, today=None):
    """The ensemble for the panel's `top` (pi ids, best first, as many as the
    largest system runs): the current one when the models every system runs
    are the same, else the next number, registered. -> (ensemble, moved)."""
    by_size, current = sizes(reg), ensemble(reg)
    if _signature(top, by_size) == _signature(current["members"], by_size):
        return current, False
    version = current["version"] + 1
    if version > len(reg["places"]):
        sys.exit("every place in the registry is used; add places")
    new = {"version": version, "place": reg["places"][version - 1], "members": list(top),
           "registered": today or date.today().isoformat()}
    reg["ensembles"].append(new)
    return new, True


def name_for(reg, system, ens):
    """The name `system` takes on ensemble `ens`: <system>-<place>, or the next
    revision when that name is already registered."""
    base = f"{system}-{ens['place']}"
    if base not in reg["names"]:
        return base
    revision = 2
    while f"{base}-{revision}" in reg["names"]:
        revision += 1
    return f"{base}-{revision}"


def current(reg, system):
    """The name `system` goes out under now."""
    if system not in reg["systems"]:
        sys.exit(f"no system {system!r} in the registry (it has {', '.join(reg['systems'])})")
    return reg["systems"][system]["current"]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    reg = load()
    if argv[:1] == ["current"] and len(argv) == 2:
        print(current(reg, argv[1]))
    elif argv[:1] == ["show"]:
        ens = ensemble(reg)
        print(f"ensemble {ens['version']} ({ens['place']}): {', '.join(ens['members'])}")
        for name, s in reg["systems"].items():
            print(f"{s['current']:22} {s['condition']:28} top {s['size']}: {s['description']}")
    else:
        sys.exit("usage: python -m fri_forecaster.systems show | current <system>")


if __name__ == "__main__":
    main()
