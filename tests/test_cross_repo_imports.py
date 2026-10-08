import json
import subprocess

from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
from kp_agent_tooling._impl.service.import_context import _ResolutionBudget, import_context


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def commit_repo(root, files, message="fixture"):
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "config", "user.name", "Fixture")
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(root, "add", ".")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def paired_repos(tmp_path, requirement="kp-core[all]==1.2.3\n"):
    core = tmp_path / "core"
    pinned = commit_repo(core, {
        "kp_core/domains/recruiting/import_bullhorn_api.py":
            "def run_import():\n    return 'pinned'\n",
    })
    git(core, "tag", "-a", "v1.2.3", "-m", "release")
    (core / "kp_core/domains/recruiting/import_bullhorn_api.py").write_text(
        "def run_import():\n    return 'dev'\n")
    git(core, "add", ".")
    git(core, "commit", "-qm", "dev differs")
    dev = git(core, "rev-parse", "HEAD")

    ats = tmp_path / "ats"
    consumer = commit_repo(ats, {
        "requirements.txt": requirement,
        "deploy/manifest/import_bullhorn.py":
            "from kp_core.domains.recruiting.import_bullhorn_api import run_import\n\nrun_import()\n",
    })
    return ats, consumer, core, pinned, dev


def config(tmp_path, ats, consumer, core, dev):
    path = tmp_path / "tooling.json"
    path.write_text(json.dumps({
        "schema_version": "ops.agent-tooling.v1",
        "repos": {
            "ats": {"path": str(ats), "revision": consumer},
            "core": {"path": str(core), "revision": dev},
        },
        "import_mappings": {
            "kp_core": {"repo_key": "core", "distribution": "kp-core"},
        },
    }))
    return path


def test_public_import_schema_and_adapter_follow_committed_pin(tmp_path):
    """GREEN-IF mapped imports resolve at the committed exact pin and emit a usable source call."""
    ats, consumer, core, pinned, dev = paired_repos(tmp_path)
    adapter = AgentTooling(config(tmp_path, ats, consumer, core, dev))
    schema = next(t for t in adapter.tools() if t["name"] == "navigation.imports")["inputSchema"]
    assert schema["properties"]["dependency_revisions"]["additionalProperties"]["pattern"]

    report = adapter.call("navigation.imports", {
        "repo_key": "ats", "path": "deploy/manifest/import_bullhorn.py",
    })
    row = report["imports"][0]
    assert row["binding_status"] == "resolved_cross_repo"
    assert row["target_repo_key"] == "core"
    assert row["target_revision"] == pinned != dev
    assert row["selection"] == "pinned"
    assert row["path"] == "kp_core/domains/recruiting/import_bullhorn_api.py"
    assert row["start_line"] == 1
    assert row["revision_provenance"] == {
        "pin_file": "requirements.txt",
        "pin_blob_sha": report["dependency_pins"]["core"]["pin_blob_sha"],
        "package": "kp-core", "version": "1.2.3", "tag": "v1.2.3",
        "resolved_commit": pinned,
    }
    follow = row["next_call"]
    assert follow["tool"] == "navigation.source"
    source_report = adapter.call(follow["tool"], follow["arguments"])
    assert source_report["source_revision"] == pinned
    assert "def run_import" in source_report["excerpt"]


def test_explicit_dependency_revision_precedes_pin_and_is_labeled(tmp_path):
    """GREEN-IF an explicit full dependency revision wins and its provenance is visible."""
    ats, consumer, core, pinned, dev = paired_repos(tmp_path)
    adapter = AgentTooling(config(tmp_path, ats, consumer, core, dev))
    row = adapter.call("navigation.imports", {
        "repo_key": "ats", "path": "deploy/manifest/import_bullhorn.py",
        "dependency_revisions": {"core": dev},
    })["imports"][0]
    assert row["selection"] == "explicit" and row["target_revision"] == dev
    assert row["revision_provenance"] == {"resolved_commit": dev}


def test_pin_failures_never_fall_back_to_configured_dev(tmp_path):
    """GREEN-IF ranges, markers, conflicts and unavailable tags stay unresolved with reasons."""
    cases = [
        ("kp-core>=1.2\n", "unsupported_pin_specifier"),
        ("kp-core==1.2.*\n", "unsupported_pin_specifier"),
        ("kp-core==1.2.3,>=1\n", "unsupported_pin_specifier"),
        ("kp-core===1.2.3\n", "unsupported_pin_specifier"),
        ("kp-core==1.2.3; python_version > '3.10'\n", "unsupported_pin_marker"),
        ("kp-core==1.2.3\nkp-core==2.0.0\n", "conflicting_exact_pins"),
        ("kp-core==9.9.9\n", "pin_tag_unavailable"),
        ("-r more.txt\n", "unsupported_requirements_include"),
        ("-rmore.txt\n", "unsupported_requirements_include"),
        ("-c constraints.txt\nkp-core==1.2.3\n", "unsupported_requirements_include"),
        ("-r more.txt\nkp-core==1.2.3\n", "unsupported_requirements_include"),
    ]
    for index, (requirement, reason) in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        ats, consumer, core, _, dev = paired_repos(case, requirement)
        adapter = AgentTooling(config(case, ats, consumer, core, dev))
        row = adapter.call("navigation.imports", {
            "repo_key": "ats", "path": "deploy/manifest/import_bullhorn.py",
        })["imports"][0]
        assert row["binding_status"] == "unresolved"
        assert row["resolution_reason"] == reason
        assert "target_revision" not in row
        assert row["next_call"]["arguments"]["repo_key"] == "ats"


def test_unmapped_module_and_name_only_similarity_do_not_join(tmp_path):
    """GREEN-IF only the operator mapping can create a cross-repository join."""
    ats, consumer, core, _, dev = paired_repos(tmp_path)
    cfg = config(tmp_path, ats, consumer, core, dev)
    model = json.loads(cfg.read_text())
    model["import_mappings"] = {}
    cfg.write_text(json.dumps(model))
    row = AgentTooling(cfg).call("navigation.imports", {
        "repo_key": "ats", "path": "deploy/manifest/import_bullhorn.py",
    })["imports"][0]
    assert row["binding_status"] == "unresolved"
    assert row["next_call"]["arguments"]["repo_key"] == "ats"


def test_import_mapping_validation_and_adapter_forwarding(tmp_path, monkeypatch):
    """GREEN-IF invalid mappings fail closed and adapter forwards mappings and overrides."""
    ats, consumer, core, _, dev = paired_repos(tmp_path)
    cfg = config(tmp_path, ats, consumer, core, dev)
    model = json.loads(cfg.read_text())
    model["import_mappings"]["kp_core"]["repo_key"] = "missing"
    cfg.write_text(json.dumps(model))
    with __import__("pytest").raises(ValueError, match="import mapping"):
        AgentTooling(cfg)

    cfg = config(tmp_path, ats, consumer, core, dev)
    observed = {}
    def fake(*args, **kwargs):
        observed.update(kwargs)
        return {"status": "ok"}
    monkeypatch.setattr("kp_agent_tooling._impl.service.agent_tooling.import_context", fake)
    AgentTooling(cfg).call("navigation.imports", {
        "repo_key": "ats", "path": "deploy/manifest/import_bullhorn.py",
        "dependency_revisions": {"core": dev},
    })
    assert observed["repositories"]["core"]["revision"] == dev
    assert observed["import_mappings"]["kp_core"]["distribution"] == "kp-core"
    assert observed["dependency_revisions"] == {"core": dev}


def test_adapter_selects_one_profile_per_import_call(tmp_path, monkeypatch):
    """GREEN-IF one request cannot mix consumer and dependency profiles."""
    ats,consumer,core,_,dev=paired_repos(tmp_path)
    cfg=config(tmp_path,ats,consumer,core,dev)
    from kp_agent_tooling._impl.navigation_workspace import active as real_active
    calls=[]
    def counted(model):
        calls.append(1)
        return real_active(model)
    monkeypatch.setattr("kp_agent_tooling._impl.navigation_workspace.active",counted)
    AgentTooling(cfg).call("navigation.imports",{
        "repo_key":"ats","path":"deploy/manifest/import_bullhorn.py",
        "target_revision":consumer})
    assert len(calls) == 1


def test_direct_import_context_rejects_relative_cross_repo_mapping(monkeypatch):
    """GREEN-IF relative imports never escape their consumer repository."""
    monkeypatch.setattr("kp_agent_tooling._impl.service.import_context.source", lambda *a, **k: ("b" * 40, "from .kp_core import run_import\n"))
    report = import_context("unused", "a" * 40, "ats", "route.py", None, [],
        repositories={"core": {"path": "unused", "revision": "c" * 40}},
        import_mappings={"kp_core": {"repo_key": "core", "distribution": "kp-core"}})
    assert report["imports"][0]["binding_status"] == "unresolved"


def test_duplicate_or_reexported_target_is_not_claimed_as_declaration(tmp_path):
    """GREEN-IF ambiguous bindings and reexports remain unresolved."""
    for index, target in enumerate([
        "def run_import(): pass\ndef run_import(): pass\n",
        "from elsewhere import run_import\n",
    ]):
        case=tmp_path/str(index);case.mkdir()
        ats,consumer,core,_,dev=paired_repos(case)
        path=core/"kp_core/domains/recruiting/import_bullhorn_api.py"
        path.write_text(target);git(core,"add",".");git(core,"commit","-qm","ambiguous")
        git(core,"tag","-f","v1.2.3")
        adapter=AgentTooling(config(case,ats,consumer,core,dev))
        row=adapter.call("navigation.imports",{
            "repo_key":"ats","path":"deploy/manifest/import_bullhorn.py"})["imports"][0]
        assert row["binding_status"] == "unresolved"
        assert row["resolution_reason"] in {
            "ambiguous_declaration", "reexport_outside_mapped_root",
        }


def test_resolution_is_limited_and_pin_selection_cached(monkeypatch):
    """GREEN-IF omitted imports do no cross-repo work and one target pin is selected once."""
    text='\n'.join(f'from kp_core.mod{i} import value' for i in range(4))+'\n'
    monkeypatch.setattr("kp_agent_tooling._impl.service.import_context.source",lambda *a,**k:("b"*40,text))
    calls=[]
    monkeypatch.setattr("kp_agent_tooling._impl.service.import_context._select_revision",
        lambda *a,**k:(calls.append(1) or (None,"pin_tag_unavailable")))
    result=import_context("consumer","a"*40,"ats","route.py",None,[],limit=2,
        repositories={"core":{"path":"core","revision":"c"*40}},
        import_mappings={"kp_core":{"repo_key":"core","distribution":"kp-core"}})
    assert len(result["imports"]) == 2 and result["omitted"] == 2
    assert len(calls) == 1


def test_package_reexport_alias_resolves_to_unique_source_declaration(tmp_path):
    """GREEN-IF a bounded package re-export alias reaches its unique implementation."""
    ats, consumer, core, _, dev = paired_repos(tmp_path)
    (core / "kp_core/public").mkdir(parents=True)
    (core / "kp_core/public/__init__.py").write_text(
        "from .implementation import run as run_import\n")
    (core / "kp_core/public/implementation.py").write_text(
        "def run():\n    return 'source-backed'\n")
    git(core, "add", ".")
    git(core, "commit", "-qm", "package reexport")
    git(core, "tag", "-f", "v1.2.3")
    (ats / "deploy/manifest/import_bullhorn.py").write_text(
        "from kp_core.public import run_import as execute\nexecute()\n")
    git(ats, "add", ".")
    git(ats, "commit", "-qm", "consume reexport")
    consumer = git(ats, "rev-parse", "HEAD")

    row = AgentTooling(config(tmp_path, ats, consumer, core, dev)).call(
        "navigation.imports", {
            "repo_key": "ats", "path": "deploy/manifest/import_bullhorn.py",
        })["imports"][0]
    assert row["local_name"] == "execute"
    assert row["binding_status"] == "resolved_cross_repo"
    assert row["path"] == "kp_core/public/implementation.py"
    assert row["start_line"] == 1
    assert row["declaration_kind"] == "function"
    assert row["reexport_chain"] == [{
        "module": "kp_core.public", "path": "kp_core/public/__init__.py",
        "line": 1, "imported_name": "run", "exported_name": "run_import",
    }]


def test_module_file_and_package_layout_is_ambiguous(tmp_path):
    """GREEN-IF module.py plus module/__init__.py never uses first-readable guessing."""
    ats, consumer, core, _, dev = paired_repos(tmp_path)
    module = core / "kp_core/domains/recruiting/import_bullhorn_api"
    module.mkdir()
    (module / "__init__.py").write_text("def run_import(): return 'package'\n")
    git(core, "add", ".")
    git(core, "commit", "-qm", "ambiguous module layout")
    git(core, "tag", "-f", "v1.2.3")
    row = AgentTooling(config(tmp_path, ats, consumer, core, dev)).call(
        "navigation.imports", {
            "repo_key": "ats", "path": "deploy/manifest/import_bullhorn.py",
        })["imports"][0]
    assert row["binding_status"] == "unresolved"
    assert row["resolution_reason"] == "ambiguous_module_layout"


def test_star_dynamic_cycle_and_conditional_exports_are_explicit(tmp_path):
    """GREEN-IF unsafe export forms have stable, distinct unresolved reasons."""
    targets = [
        ("from .implementation import *\n", "star_reexport_unsupported"),
        ("def run_import(): pass\nfrom .implementation import *\n", "star_reexport_unsupported"),
        ("def __getattr__(name): return object()\n", "dynamic_exports_unsupported"),
        ("from .other import run_import\n", "declaration_reexport_cycle"),
        ("if True:\n    def run_import(): pass\n", "conditional_declaration_unsupported"),
    ]
    for index, (target, reason) in enumerate(targets):
        case = tmp_path / str(index)
        case.mkdir()
        ats, consumer, core, _, dev = paired_repos(case)
        path = core / "kp_core/domains/recruiting/import_bullhorn_api.py"
        path.write_text(target)
        if "other" in target:
            (path.parent / "other.py").write_text(
                "from .import_bullhorn_api import run_import\n")
        git(core, "add", ".")
        git(core, "commit", "-qm", "unsupported export")
        git(core, "tag", "-f", "v1.2.3")
        row = AgentTooling(config(case, ats, consumer, core, dev)).call(
            "navigation.imports", {
                "repo_key": "ats", "path": "deploy/manifest/import_bullhorn.py",
            })["imports"][0]
        assert row["binding_status"] == "unresolved"
        assert row["resolution_reason"] == reason


def test_resolution_budget_is_shared_across_recursive_reads(tmp_path):
    """GREEN-IF one aggregate file budget stops a re-export chain with visible usage."""
    ats, consumer, core, _, dev = paired_repos(tmp_path)
    base = core / "kp_core/domains/recruiting"
    (base / "import_bullhorn_api.py").write_text("from .one import run_import\n")
    (base / "one.py").write_text("from .two import run_import\n")
    (base / "two.py").write_text("def run_import(): pass\n")
    git(core, "add", ".")
    git(core, "commit", "-qm", "deep reexport")
    git(core, "tag", "-f", "v1.2.3")
    report = import_context(
        str(ats), consumer, "ats", "deploy/manifest/import_bullhorn.py", None, [],
        repositories={"core": {"path": str(core), "revision": dev}},
        import_mappings={"kp_core": {"repo_key": "core", "distribution": "kp-core"}},
        resolution_limits={"files": 3, "source_bytes": 2_000_000,
                           "recursion": 8, "seconds": 10.0},
    )
    row = report["imports"][0]
    assert row["binding_status"] == "unresolved"
    assert row["resolution_reason"] == "resolution_file_budget_exceeded"
    assert report["resolution_usage"]["files"] == 3
    assert report["resolution_limits"]["files"] == 3


def test_resolution_byte_and_recursion_budgets_are_explicit(tmp_path):
    """GREEN-IF aggregate bytes and recursive re-exports stop with distinct reasons."""
    ats, consumer, core, _, dev = paired_repos(tmp_path)
    base = core / "kp_core/domains/recruiting"
    (base / "import_bullhorn_api.py").write_text("from .one import run_import\n")
    (base / "one.py").write_text("def run_import(): pass\n")
    git(core, "add", ".")
    git(core, "commit", "-qm", "recursive reexport")
    git(core, "tag", "-f", "v1.2.3")
    common = dict(
        repositories={"core": {"path": str(core), "revision": dev}},
        import_mappings={"kp_core": {"repo_key": "core", "distribution": "kp-core"}},
    )
    byte_row = import_context(
        str(ats), consumer, "ats", "deploy/manifest/import_bullhorn.py", None, [],
        resolution_limits={"files": 16, "source_bytes": 1,
                           "recursion": 8, "seconds": 10.0}, **common,
    )["imports"][0]
    assert byte_row["resolution_reason"] == "resolution_source_byte_budget_exceeded"
    recursion_row = import_context(
        str(ats), consumer, "ats", "deploy/manifest/import_bullhorn.py", None, [],
        resolution_limits={"files": 16, "source_bytes": 2_000_000,
                           "recursion": 1, "seconds": 10.0}, **common,
    )["imports"][0]
    assert recursion_row["resolution_reason"] == "resolution_recursion_budget_exceeded"


def test_resolution_elapsed_budget_is_shared_and_visible(tmp_path, monkeypatch):
    """GREEN-IF expiry before the first dependency read performs no later reads."""
    ats, consumer, core, _, dev = paired_repos(tmp_path)
    ticks = iter([0.0, 0.1, 0.2, 0.3])
    monkeypatch.setattr("kp_agent_tooling._impl.service.import_context.time.monotonic", lambda: next(ticks))
    report = import_context(
        str(ats), consumer, "ats", "deploy/manifest/import_bullhorn.py", None, [],
        repositories={"core": {"path": str(core), "revision": dev}},
        import_mappings={"kp_core": {"repo_key": "core", "distribution": "kp-core"}},
        resolution_limits={"files": 16, "source_bytes": 2_000_000,
                           "recursion": 8, "seconds": 0.05},
    )
    assert report["imports"][0]["resolution_reason"] == "resolution_elapsed_budget_exceeded"
    assert report["resolution_usage"]["files"] == 0


def test_parser_overrun_cannot_return_a_resolved_declaration(tmp_path, monkeypatch):
    """GREEN-IF cooperative expiry during target parsing is checked before success."""
    ats, consumer, core, _, dev = paired_repos(tmp_path)
    import kp_agent_tooling._impl.service.import_context as module
    real_parse = module.ast.parse
    clock = {"now": 0.0}
    def parse_then_advance(text, *args, **kwargs):
        tree = real_parse(text, *args, **kwargs)
        if "def run_import" in text:
            clock["now"] = 11.0
        return tree
    monkeypatch.setattr(module.ast, "parse", parse_then_advance)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])
    report = import_context(
        str(ats), consumer, "ats", "deploy/manifest/import_bullhorn.py", None, [],
        repositories={"core": {"path": str(core), "revision": dev}},
        import_mappings={"kp_core": {"repo_key": "core", "distribution": "kp-core"}},
        resolution_limits={"files": 16, "source_bytes": 2_000_000,
                           "recursion": 8, "seconds": 10.0},
    )
    row = report["imports"][0]
    assert row["binding_status"] == "unresolved"
    assert row["resolution_reason"] == "resolution_elapsed_budget_exceeded"


def test_pinned_continuation_does_not_inherit_incompatible_snapshot(tmp_path):
    """GREEN-IF pinned Core provenance survives while the source call omits a dev snapshot."""
    ats, consumer, core, pinned, dev = paired_repos(tmp_path)
    adapter = AgentTooling(config(tmp_path, ats, consumer, core, dev))
    row = adapter.call("navigation.imports", {
        "repo_key": "ats", "path": "deploy/manifest/import_bullhorn.py",
    })["imports"][0]
    assert row["target_revision"] == pinned != dev
    assert row["revision_provenance"]["resolved_commit"] == pinned
    assert row["configured_target_revision"] == dev
    assert row["continuation_snapshot_compatible"] is False
    assert "snapshot_id" not in row["next_call"]["arguments"]
    assert adapter.call(row["next_call"]["tool"], row["next_call"]["arguments"])[
        "source_revision"] == pinned


def test_unconfigured_target_fails_closed_without_reading_source(monkeypatch):
    """GREEN-IF an unconfigured mapped repository is unresolved before target reads."""
    consumer = "from kp_core.public import run_import\n"
    calls = []
    def fake_source(repo, revision, path, **kwargs):
        calls.append((repo, path))
        return "b" * 40, consumer
    monkeypatch.setattr("kp_agent_tooling._impl.service.import_context.source", fake_source)
    report = import_context(
        "consumer", "a" * 40, "ats", "route.py", None, [],
        repositories={},
        import_mappings={"kp_core": {"repo_key": "core", "distribution": "kp-core"}},
    )
    assert report["imports"][0]["resolution_reason"] == "target_repo_unconfigured"
    assert calls == [("consumer", "route.py")]


def test_unmapped_dependency_override_is_not_authorized(tmp_path):
    """GREEN-IF configured repository membership alone cannot authorize an override."""
    ats, consumer, core, _, dev = paired_repos(tmp_path)
    cfg = config(tmp_path, ats, consumer, core, dev)
    model = json.loads(cfg.read_text())
    model["repos"]["other"] = model["repos"]["core"]
    cfg.write_text(json.dumps(model))
    with __import__("pytest").raises(ValueError, match="not authorized"):
        AgentTooling(cfg).call("navigation.imports", {
            "repo_key": "ats", "path": "deploy/manifest/import_bullhorn.py",
            "dependency_revisions": {"other": dev},
        })


def test_direct_module_alias_preserves_leading_lines_and_crlf(tmp_path):
    """GREEN-IF direct module aliases resolve exact bytes without stripped line shifts."""
    ats, consumer, core, _, dev = paired_repos(tmp_path)
    module = core / "kp_core/direct.py"
    module.write_bytes(b"\r\n\r\nVALUE = 1\r\n")
    git(core, "add", ".")
    git(core, "commit", "-qm", "direct module")
    git(core, "tag", "-f", "v1.2.3")
    consumer_path = ats / "deploy/manifest/import_bullhorn.py"
    consumer_path.write_text("import kp_core.direct as direct_alias\ndirect_alias.VALUE\n")
    git(ats, "add", ".")
    git(ats, "commit", "-qm", "direct alias")
    consumer = git(ats, "rev-parse", "HEAD")
    adapter = AgentTooling(config(tmp_path, ats, consumer, core, dev))
    row = adapter.call("navigation.imports", {
        "repo_key": "ats", "path": "deploy/manifest/import_bullhorn.py",
    })["imports"][0]
    assert row["local_name"] == "direct_alias"
    assert row["binding_status"] == "resolved_cross_repo"
    assert row["declaration_kind"] == "module"
    assert row["path"] == "kp_core/direct.py"
    follow = adapter.call(row["next_call"]["tool"], row["next_call"]["arguments"])
    assert follow["start_line"] == 1
    assert follow["excerpt"].startswith("\n\nVALUE = 1")


def test_direct_declaration_with_conditional_shadow_stays_unresolved(tmp_path):
    """GREEN-IF a direct definition plus conditional rebinding is not called unique."""
    ats, consumer, core, _, dev = paired_repos(tmp_path)
    path = core / "kp_core/domains/recruiting/import_bullhorn_api.py"
    path.write_text("def run_import(): pass\nif FLAG:\n    run_import = replacement\n")
    git(core, "add", ".")
    git(core, "commit", "-qm", "conditional shadow")
    git(core, "tag", "-f", "v1.2.3")
    row = AgentTooling(config(tmp_path, ats, consumer, core, dev)).call(
        "navigation.imports", {
            "repo_key": "ats", "path": "deploy/manifest/import_bullhorn.py",
        })["imports"][0]
    assert row["binding_status"] == "unresolved"
    assert row["resolution_reason"] == "conditional_declaration_unsupported"


def test_reexport_cannot_leave_operator_mapped_module_root(tmp_path):
    """GREEN-IF a re-export cannot traverse an unrelated namespace in the target repo."""
    ats, consumer, core, _, dev = paired_repos(tmp_path)
    path = core / "kp_core/domains/recruiting/import_bullhorn_api.py"
    path.write_text("from unrelated import run_import\n")
    (core / "unrelated.py").write_text("def run_import(): pass\n")
    git(core, "add", ".")
    git(core, "commit", "-qm", "outside root")
    git(core, "tag", "-f", "v1.2.3")
    row = AgentTooling(config(tmp_path, ats, consumer, core, dev)).call(
        "navigation.imports", {
            "repo_key": "ats", "path": "deploy/manifest/import_bullhorn.py",
        })["imports"][0]
    assert row["binding_status"] == "unresolved"
    assert row["resolution_reason"] == "reexport_outside_mapped_root"


def test_bounded_target_reader_ignores_ambient_git_redirection(tmp_path, monkeypatch):
    """GREEN-IF target reads stay in the configured repository under hostile GIT_* vars."""
    ats, _, core, _, dev = paired_repos(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(ats / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(ats))
    budget = _ResolutionBudget.create(None)
    _, text = budget.read(
        str(core), dev, "kp_core/domains/recruiting/import_bullhorn_api.py")
    assert "return 'dev'" in text
