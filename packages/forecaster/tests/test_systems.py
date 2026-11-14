"""The name registry's numbering (fri_forecaster.systems): one ensemble number for every system."""
import copy
import io
import json
from contextlib import redirect_stdout

import pytest

from fri_forecaster import systems

REG = {"places": ["oak", "elm", "ash"],
       "ensembles": [{"version": 1, "place": "oak", "members": ["a/one", "b/two", "c/three", "d/four"], "registered": "2026-01-01"}],
       "systems": {"quartet": {"description": "the top 4", "condition": "baseline", "size": 4, "current": "quartet-oak", "public": True},
                   "solo": {"description": "the top model", "condition": "baseline", "size": 1, "current": "solo-oak"}},
       "names": {"quartet-oak": {}, "solo-oak": {}}}


@pytest.fixture
def reg():
    return copy.deepcopy(REG)


def test_the_same_models_at_every_size_are_the_same_ensemble(reg):
    assert systems.advance(reg, ["a/one", "c/three", "b/two", "d/four"]) == (REG["ensembles"][0], False)
    assert len(reg["ensembles"]) == 1


def test_a_changed_member_or_a_new_top_model_is_the_next_number_and_place(reg):
    ens, moved = systems.advance(reg, ["a/one", "b/two", "c/three", "e/five"], today="2026-02-02")
    assert moved and ens == {"version": 2, "place": "elm", "members": ["a/one", "b/two", "c/three", "e/five"], "registered": "2026-02-02"}
    ens, moved = systems.advance(reg, ["b/two", "a/one", "c/three", "e/five"])       # the same four, a new top model
    assert moved and (ens["version"], ens["place"]) == (3, "ash")
    assert systems.ensemble(reg) is ens
    with pytest.raises(SystemExit, match="every place"):
        systems.advance(reg, ["z/new", "a/one", "c/three", "e/five"])


def test_a_name_is_the_system_and_the_ensembles_place_then_revisions(reg):
    ens = systems.ensemble(reg)
    assert systems.name_for(reg, "trio", ens) == "trio-oak"
    assert systems.name_for(reg, "solo", ens) == "solo-oak-2"
    reg["names"]["solo-oak-2"] = {}
    assert systems.name_for(reg, "solo", ens) == "solo-oak-3"
    assert systems.current(reg, "quartet") == "quartet-oak"
    with pytest.raises(SystemExit, match="no system 'trio'"):
        systems.current(reg, "trio")


def test_the_registry_is_read_from_one_place_and_the_old_shape_is_refused(reg, tmp_path, monkeypatch):
    path = tmp_path / "names.json"
    monkeypatch.setenv("FORECASTER_NAMES", str(path))
    with pytest.raises(SystemExit, match="no name registry"):
        systems.load()
    path.write_text(json.dumps({"places": [], "conditions": {}, "names": {}}))
    with pytest.raises(SystemExit, match="old shape"):
        systems.load()
    systems.save(reg)
    assert systems.load() == reg
    out = io.StringIO()
    with redirect_stdout(out):
        systems.main(["current", "solo"])
        systems.main(["show"])
    assert out.getvalue().splitlines()[:2] == ["solo-oak", "ensemble 1 (oak): a/one, b/two, c/three, d/four"]


def test_a_panel_is_names_with_or_without_ids():
    panel = systems.parse_panel(["Model One\ta/one\tLab A\n", "Model Two\n", "\n"])
    assert panel == [{"eci_name": "Model One", "pi_model": "a/one", "lab": "Lab A"},
                     {"eci_name": "Model Two", "pi_model": None, "lab": None}]
