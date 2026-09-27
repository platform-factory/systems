#!/usr/bin/env python3
"""The systems repo's check: the tenant files, and the list of retired names.

Why it exists
-------------
Every file under tenants/ becomes a tenant. Today the API server checks each
one against the System XRD when Argo CD applies it. From M2b's engine swap it
will not: a tenant file stops being a Kubernetes object and becomes values
that platform-config's charts/system renders (ADR-0017 §3). So the file's
shape is checked here, on every pull request, together with two rules the API
server never knew: one file per System, named after it, and the retired-name
list (ADR-0017 §9, ADR-0019 §3).

This check is advisory. No repo in the org requires a passing check yet
(ADR-0018 §7), so a red result makes a mistake visible; it does not block the
merge. Reserved and colliding System names (ADR-0017 §6) are deliberately
not checked here. That list lives once, in charts/system's schema.

The rules, each with the id it reports
--------------------------------------
  [flat]                tenants/ holds only *.yaml files, and no subfolders.
  [one-document]        each tenant file is exactly one YAML mapping.
  [top-level-keys]      its top-level keys are exactly apiVersion, kind,
                        metadata and spec.
  [kind]                apiVersion and kind are the System's.
  [metadata]            metadata holds a name and nothing else.
  [name-pattern]        the name follows the System XRD's rule.
  [name-matches-file]   tenants/<name>.yaml holds metadata.name: <name>.
  [spec]                spec's fields follow the System XRD.
  [retired-format]      retired.yaml holds `retired:` and a list of names.
  [retired-in-use]      no retired name still has a tenant file.
  [removed-not-retired] on a pull request, every tenant file the PR removes
                        or renames has its name added to retired.yaml.

How to run it
-------------
  python3 scripts/check_tenants.py                # check this repo
  python3 scripts/check_tenants.py --base DIR     # also compare with DIR,
                                                  # which holds the base
                                                  # branch's tenants/
  python3 scripts/check_tenants.py --self-test    # run the fixtures in
                                                  # tests/check-fixtures/
"""

import argparse
import pathlib
import re
import sys

import yaml

# Copied from the System XRD in platform-config
# (crossplane/compositions/system/xrd.yaml), which is the rule the API server
# applies today. A System's name becomes a GCP service account ID, which
# allows 6-30 characters of exactly this shape.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
# A team name is both a Google Group local part and a Kubernetes label value.
TEAM_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
REPO_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
API_VERSION = "platform.thecloudgeek.io/v1alpha1"
KIND = "System"
TOP_LEVEL_KEYS = {"apiVersion", "kind", "metadata", "spec"}
SPEC_KEYS = {"owner", "tier", "securityTier", "size"}
REQUIRED_SPEC_KEYS = ["owner", "securityTier", "size"]
ENUMS = {
    "tier": ["standard", "critical"],
    "securityTier": ["internal", "restricted"],
    "size": ["S", "M", "L"],
}

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "check-fixtures"


def check_repo(repo, base=None):
    """Check one checkout. Returns a list of (rule, message); empty means it passes.

    `base`, when given, is a folder holding the base branch's tenants/ (and
    retired.yaml, if the base had one). It is used only to find the tenant
    files this change removes or renames.
    """
    problems = []
    tenants_dir = repo / "tenants"
    files_in_use = set()  # file names without .yaml
    if not tenants_dir.is_dir():
        return [("flat", "tenants/ is missing")]

    for path in sorted(tenants_dir.iterdir()):
        where = f"tenants/{path.name}"
        if path.is_dir():
            # Argo CD's file generator matches *.yaml at any depth, so a file
            # in a subfolder would still become a tenant (ADR-0017 §3).
            problems.append(("flat", f"{where}/: tenants/ must stay flat, with no subfolders"))
            continue
        if path.suffix != ".yaml":
            # The generator matches *.yaml only, so a .yml file would be
            # ignored without a word.
            problems.append(("flat", f"{where}: only *.yaml files belong in tenants/"))
            continue
        files_in_use.add(path.stem)
        problems += check_tenant_file(path, where)

    retired, retired_problems = read_retired(repo / "retired.yaml")
    problems += retired_problems
    for name in sorted(retired & files_in_use):
        problems.append((
            "retired-in-use",
            f"retired.yaml: {name} is retired, but tenants/{name}.yaml still exists. "
            "A retired name is refused at render, so this tenant would stop rendering.",
        ))

    if base is not None:
        problems += check_removals(base, files_in_use, retired)
        for note in notes_on_unretired(base, retired):
            print(f"note {note}")

    return problems


def check_tenant_file(path, where):
    """Check one file in tenants/."""
    try:
        documents = [d for d in yaml.safe_load_all(path.read_text()) if d is not None]
    except yaml.YAMLError as error:
        return [("one-document", f"{where}: does not parse as YAML: {error}")]
    if len(documents) != 1 or not isinstance(documents[0], dict):
        return [("one-document", f"{where}: must hold exactly one YAML mapping, found {len(documents)} document(s)")]
    tenant = documents[0]

    problems = []
    keys = {str(key) for key in tenant}
    if keys != TOP_LEVEL_KEYS:
        problems.append((
            "top-level-keys",
            f"{where}: top-level keys must be exactly apiVersion, kind, metadata and spec; found {sorted(keys)}",
        ))
    if tenant.get("apiVersion") != API_VERSION or tenant.get("kind") != KIND:
        problems.append(("kind", f"{where}: must be apiVersion {API_VERSION}, kind {KIND}"))

    metadata = tenant.get("metadata")
    if not isinstance(metadata, dict) or set(metadata) != {"name"}:
        problems.append(("metadata", f"{where}: metadata must hold a name and nothing else"))
    else:
        name = metadata["name"]
        if not isinstance(name, str) or not NAME_PATTERN.match(name):
            problems.append((
                "name-pattern",
                f"{where}: name {name!r} must be 6-30 characters, start with a lowercase letter, "
                "end with a letter or digit, and use only lowercase letters, digits and dashes",
            ))
        if name != path.stem:
            problems.append(("name-matches-file", f"{where}: metadata.name is {name!r}, so the file must be tenants/{name}.yaml"))

    problems += check_spec(tenant.get("spec"), where)
    return problems


def check_spec(spec, where):
    """Check spec against the System XRD's rules."""
    if not isinstance(spec, dict):
        return [("spec", f"{where}: spec must be a mapping")]
    problems = []
    for key in sorted({str(key) for key in spec} - SPEC_KEYS):
        problems.append(("spec", f"{where}: spec.{key} is not a System field"))
    for key in REQUIRED_SPEC_KEYS:
        if key not in spec:
            problems.append(("spec", f"{where}: spec.{key} is required"))

    owner = spec.get("owner")
    if owner is not None:
        if not isinstance(owner, dict) or set(owner) != {"team", "repo"}:
            problems.append(("spec", f"{where}: spec.owner must hold exactly team and repo"))
        else:
            team, repo = owner["team"], owner["repo"]
            if not (isinstance(team, str) and 2 <= len(team) <= 63 and TEAM_PATTERN.match(team)):
                problems.append(("spec", f"{where}: spec.owner.team {team!r} must be 2-63 lowercase letters, digits and dashes"))
            if not (isinstance(repo, str) and len(repo) <= 128 and REPO_PATTERN.match(repo)):
                problems.append(("spec", f"{where}: spec.owner.repo {repo!r} must be org/name"))

    for key, allowed in ENUMS.items():
        if key in spec and spec[key] not in allowed:
            problems.append(("spec", f"{where}: spec.{key} must be one of {allowed}, found {spec[key]!r}"))
    return problems


def read_retired(path):
    """Return (set of retired names, problems)."""
    if not path.exists():
        return set(), [("retired-format", "retired.yaml is missing; it belongs at the repo root")]
    try:
        document = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        return set(), [("retired-format", f"retired.yaml does not parse as YAML: {error}")]
    if not isinstance(document, dict) or set(document) != {"retired"} or not isinstance(document["retired"], list):
        return set(), [("retired-format", "retired.yaml must hold one key, retired, whose value is a list (use [] for none)")]

    names, problems = set(), []
    for entry in document["retired"]:
        if not isinstance(entry, str) or not NAME_PATTERN.match(entry):
            problems.append(("retired-format", f"retired.yaml: {entry!r} is not a valid System name"))
        elif entry in names:
            problems.append(("retired-format", f"retired.yaml: {entry} is listed twice"))
        else:
            names.add(entry)
    return names, problems


def check_removals(base, files_in_use, retired):
    """Every tenant file this change removes or renames must retire its name."""
    problems = []
    base_tenants = base / "tenants"
    if not base_tenants.is_dir():
        return problems
    for path in sorted(base_tenants.glob("*.yaml")):
        if path.stem in files_in_use:
            continue  # still here; a change of metadata.name is caught by [name-matches-file]
        name = name_in(path)
        if name not in retired:
            problems.append((
                "removed-not-retired",
                f"tenants/{path.name} is removed or renamed, so add {name} to retired.yaml in this PR. "
                "Otherwise a new System could take the name and adopt the old one's database and registry.",
            ))
    return problems


def notes_on_unretired(base, retired):
    """Names leaving retired.yaml are allowed, but worth saying out loud."""
    base_retired, _ = read_retired(base / "retired.yaml")
    for name in sorted(base_retired - retired):
        yield (
            f"{name} leaves retired.yaml. That is right only once a person has deleted "
            "the last durable resource carrying the name (ADR-0015 §5)."
        )


def name_in(path):
    """metadata.name of a tenant file, or its file name if the file cannot be read."""
    try:
        tenant = yaml.safe_load(path.read_text())
        name = tenant["metadata"]["name"]
        if isinstance(name, str):
            return name
    except (yaml.YAMLError, TypeError, KeyError):
        pass
    return path.stem


def self_test():
    """Run every fixture. Returns True if each behaved as expected.

    A check that has never been seen to fail is not evidence (ADR-0019 §4), so
    every must-fail fixture has to be refused, and by the rule its `expect`
    file names. The must-pass fixtures prove the rules do not refuse
    everything.
    """
    all_ok = True
    for kind in ("must-fail", "must-pass"):
        cases = sorted(p for p in (FIXTURES / kind).iterdir() if p.is_dir())
        if not cases:
            print(f"FAIL no {kind} fixtures found under {FIXTURES / kind}")
            all_ok = False
        for case in cases:
            base = case / "base" if (case / "base").is_dir() else None
            rules = {rule for rule, _ in check_repo(case / "head", base)}
            if kind == "must-fail":
                expected = (case / "expect").read_text().strip()
                if expected in rules:
                    print(f"ok   must-fail/{case.name}: refused by [{expected}]")
                else:
                    found = ", ".join(sorted(rules)) or "nothing; it passed"
                    print(f"FAIL must-fail/{case.name}: expected [{expected}], got {found}")
                    all_ok = False
            else:
                if not rules:
                    print(f"ok   must-pass/{case.name}: passed")
                else:
                    print(f"FAIL must-pass/{case.name}: refused by {', '.join(sorted(rules))}")
                    all_ok = False
    return all_ok


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base", type=pathlib.Path, help="folder holding the base branch's tenants/")
    parser.add_argument("--self-test", action="store_true", help="run the fixtures instead of checking the repo")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    problems = check_repo(REPO_ROOT, args.base)
    for rule, message in problems:
        # GitHub shows ::error lines as annotations on the pull request.
        print(f"::error::[{rule}] {message}")
    if not problems:
        print("ok   every tenant file and retired.yaml pass")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
