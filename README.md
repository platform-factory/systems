# systems

One YAML file per tenant. Each file declares a `System` — the unit of
ownership in the Platform Factory pattern — and merging it is the entire
onboarding process. Changes here are approved by the platform team
(`CODEOWNERS` assigns everything to `@platform-factory/platform`), which is
what keeps "self-service" from meaning "unreviewed".

```
tenants/                  the live tenants — one file each, synced by Argo CD
├── svc-hello.yaml        the first tenant; owned by checkout since the C-06 move
└── svc-ledger.yaml       the second tenant, onboarded as the C-05 test
```

Only `tenants/` is applied to the cluster. The `systems` Application — a child
of `platform-config`'s root app-of-apps — sets `path: tenants`, and Argo CD
only ever reads files under that path: it resolves `<repo>/tenants` and walks
that directory and nothing above it (argo-cd v3.4.6,
`util/app/path/path.go:15` and `reposerver/repository/repository.go:2136`,
read 2026-09-16). A sibling directory such as `docs/` is outside the
Application's source entirely — not merely un-recursed — and the
`directory.recurse` flag would not change that either way. That is what let the
second tenant be prepared in `docs/` and then moved into `tenants/`: the C-05
PR was a **rename and nothing else**, one file changed.

## What a System is

**Why it exists.** A platform needs one answer to "who owns this, and what
does it get?" Without it, ownership is spread across a namespace here, an IAM
binding there, a CI config somewhere else, and a reorganization becomes a
migration project.

**The mental model.** A System is *one deployable unit with its own
namespace*, and a team is *a field on it*. That is the whole of ADR-0012, and
it is a deliberate choice against the obvious alternative (a namespace per
team, services inside it). The difference shows up the day ownership changes:

| | Team is a System | Team is a field on a System |
|---|---|---|
| Handover of a service | move it to another namespace: redeploy, new image path, new Argo project | edit one line |
| What "identity binds at one point" means | nothing much | the group is resolved in one place, and the Composition rebinds it everywhere |

So: `svc-hello` is a System named `svc-hello`. The `checkout` team owns it —
`payments` did until the C-06 move on 2026-09-16, and that move was one line in
one file. A team may own any number of Systems; a System has exactly one owning
team.
There is no `Team` kind, no team namespace, no team-level cloud resources — a
team *is* a Google Group and nothing else.

**The mechanism.** A `System` is a Crossplane Composite Resource (an XR — a
custom Kubernetes object whose fields a Composition turns into real
resources). The XRD lives in `platform-config`; Argo CD syncs this directory
into the cluster; Crossplane's Composition reads the five fields below and
materializes everything the tenant gets. Nothing in this repo names a
Kubernetes or GCP resource. The schema speaks intent; the Composition holds
the mechanism.

## The schema

```yaml
apiVersion: platform.thecloudgeek.io/v1alpha1
kind: System
metadata:
  name: svc-ledger
spec:
  owner:
    team: checkout
    repo: platform-factory/svc-ledger
  tier: standard
  securityTier: internal
  size: S
```

Eleven lines. That is the C-05 measurement, and that is the whole of
`tenants/svc-ledger.yaml` once you strip the 80 lines of explanatory comment
the file also carries.

| Field | Means | Constraints |
|---|---|---|
| `metadata.name` | The System's identity. Becomes the namespace, the Argo `AppProject`, the Artifact Registry repository, the per-System Google service account, and the `system=` label on everything composed. | 6–30 characters, lowercase letters/digits/dashes, leading letter. The floor of 6 and the ceiling of 30 come from GCP's service account ID limit; the character set is the intersection of that with a Kubernetes namespace name. Enforced at admission by a **root-level CEL rule** on the XRD (`self.metadata.name.matches(...)`), not by a `pattern` under `metadata.name` — Crossplane's CRD generator rebuilds the metadata schema and keeps only `maxLength`, silently discarding `pattern` and `minLength` (crossplane-runtime v2.3.4 `pkg/xcrd/crd.go` `genCrdVersion`, which crossplane 2.3.5 pins; read 2026-09-16). Root-level `x-kubernetes-validations` *is* copied verbatim, and Kubernetes always exposes `metadata.name` at the CEL root. Written the other way, a 4-character name would be admitted and fail halfway through the Composition at service-account creation. |
| `spec.owner.team` | The owning team, as a bare name. Resolved by convention to `<team>@thecloudgeek.io` at composition time — there is no second file mapping teams to groups, because a second file would be a second binding point. | Must be a valid Google Group local-part *and* a valid Kubernetes label value. The XRD enforces the intersection. |
| `spec.owner.repo` | The service's GitHub repo, `org/name`. The Composition points the tenant's Argo `Application` at `https://github.com/<repo>`, path `k8s/`, revision `main`. | Carried as an annotation, not a label, on composed resources: `/` is not a legal label value. |
| `spec.tier` | Service tier — what the platform owes this System when it breaks. `standard` or `critical`. | In M2 it rides along on labels. The inventory query exists before the paging policy does, on purpose. |
| `spec.securityTier` | Data sensitivity. `internal` or `restricted`. | `restricted` is reserved for tighter policy in a later milestone; both values are accepted today. |
| `spec.size` | Quota class the Composition maps to a `ResourceQuota`. `S`, `M` or `L`. | A class, never CPU numbers. That is what lets a future Composition map a System to a whole GCP project — which has no `ResourceQuota` but still has a size class — without the schema changing. |

## What one file materializes

Read this as "what a tenant gets", not as an API you write against. The
authoritative list is the System Composition in `platform-config`:

- a **namespace** named after the System, labelled with system, team, tier
  and security tier;
- a **`ResourceQuota`** from the size class;
- **two `RoleBinding`s** for the team's Google Group — the built-in `admin`
  role, and Crossplane's `crossplane-edit`, without which the team could not
  create the very `Database` claim the paved road exists to offer;
- a **`platform-system` ConfigMap** holding the team and group as plain
  strings, for humans and tooling to read;
- an Argo CD **`AppProject`** scoped to the team's repo and that one
  namespace, plus an **`Application`** syncing the repo's `k8s/` directory
  into it;
- an **Artifact Registry repository** named after the System, with the team's
  group granted write access — this is where the service's images live;
- a **Google service account** for the System plus the Workload Identity
  binding that lets the namespace's Kubernetes service account act as it, and
  the Cloud SQL client roles that make IAM database login work.

## Onboarding a tenant

One file, one PR — preceded by two prerequisites that live outside this repo
and are settled once, before the PR:

1. Ask a Workspace admin to create the team's Google Group and nest it under
   `gke-security-groups@thecloudgeek.io` — see the next section. Without it
   the `RoleBinding` applies and binds to nobody. With the group in place, the
   per-System Google service account and all four `ProjectIAMMember`s — the two
   Cloud SQL login grants among them — reported `Synced=True` on the first
   reconcile on 2026-09-16 [C]. The Artifact Registry writer member, a
   different kind composed after the repository itself, is not part of that
   observation; it reached ready along with everything else by the time the
   `System` went Ready at 16:55:04 [I]. What is still **unverified** is the other direction:
   nothing has yet asked GCP to accept a group principal that does not exist,
   so "the group is a hard prerequisite" is still the instruction rather than
   a measured consequence. Do the groups first.
2. Make sure the service repo named in `spec.owner.repo` exists and has a
   `k8s/` directory on `main`. A missing repo or path does not hang the
   `System` — the composed Argo `Application` is declared ready on existence —
   but the `Application` sits at sync status `Unknown` with a
   `ComparisonError` until the path lands (argo-cd v3.4.6,
   `util/app/path/path.go`, `controller/state.go:968`, read 2026-09-16), and
   the "usable" bar below needs something to sync.
3. Copy `tenants/svc-hello.yaml` to `tenants/<system-name>.yaml` and edit the
   five fields.
4. Open a PR. The platform team reviews it (CODEOWNERS).
5. On merge, Argo CD syncs the file, Crossplane composes the tenant, and the
   namespace is usable when the `System` reports `Ready` and a `Deployment`
   applied to the namespace is admitted under its `AppProject`.

Nothing else is a step, and steps 3–5 are the whole of what C-05 times. If
onboarding ever requires a second file in this repo, a `platform-config`
change, or a Terraform apply, that is a regression against claim C-05 and
belongs in the build log.

**Timed on 2026-09-16, for `svc-ledger`:** merged 17:02:13 → `System` object
created 17:05:10 → `System` Ready 17:06:21. **Merge → Ready: 4m08s**, and most
of that is Argo CD's roughly three-minute repository poll rather than anything
the platform does. A Running pod in the new namespace, under its own
`AppProject`, followed by about 17:07 — a placeholder unprivileged nginx,
pulled through the Docker Hub remote, since `svc-ledger` exists to test
onboarding rather than to run anything — so **merge → a tenant that can run a
workload is roughly five minutes**. From that one file came: the namespace, the `ResourceQuota`, two
`RoleBinding`s, the Kubernetes service account, the `AppProject` and
`Application` (both Synced/Healthy), the Google service account and its
Workload Identity binding, four project IAM members, and the Artifact Registry
repository with its writer member.

The first tenant, `svc-hello`, took 5m56s (created 16:49:08, Ready 16:55:04) —
slower because it absorbed two first-run problems the second tenant never saw:
a CRD that was not yet installed, and the namespace-ordering race
that the System Composition's ordering gate now closes. That difference is the
honest reading of C-05: the *second* tenant is the measurement, because the
first one is also the platform's own bring-up.

## Moving a System between teams

Edit `spec.owner.team`. That is the whole procedure, and it is claim C-06's
test.

Changing that one field changes exactly six things, all inside the
Composition:

1. the `RoleBinding` subjects in the namespace,
2. the `AppProject`'s roles,
3. the `team` label on everything the System materializes,
4. the group's IAM member on the System's Artifact Registry repository,
5. the project IAM member granting the group `roles/cloudsql.client`,
6. the project IAM member granting the group `roles/cloudsql.instanceUser`.

ADR-0012 §4 lists the first four. Items 5 and 6 come from ADR-0013 §5, written
later: humans reach their database through their own Google identity, which
means the System grants the team's group the two Cloud SQL login roles. The
two ADRs disagree on the count; ADR-0013 is the newer decision and the
Composition follows it. Raised in the M2 build log so ADR-0012 gets an
erratum rather than leaving two documents disagreeing about what C-06
measures.

One more thing carries the team and is not on that list because it is not a
grant: the `platform-system` ConfigMap in the namespace has a `group` key, a
derived convenience copy for humans and tooling. It is a plain string and
updates in place. The System Composition's header comment uses this same
numbering, so the two documents can be read against each other.

Nothing else grants on the team. The namespace, the Artifact Registry
repository, the image paths, the Argo `Application` and the service's own
identity all keep their names, because the name is the System's — not the
team's. The field that changes must not be the field that identifies; that is
also why renaming a System *is* a migration, and a team handover is not.

### What actually happens, measured 2026-09-16

The move ran for real: `svc-hello` from `payments` to `checkout`, one file
changed, one insertion, one deletion, merged 17:14:26.

**The in-cluster carriers moved in place, and quickly.** By 17:15:41 — 75
seconds after the merge, because Argo CD's poll happened to be quick — the
namespace `team` label, both `RoleBinding` subjects, the `AppProject` role
groups, the `platform-system` ConfigMap's `team`/`group` keys and the registry
object's `labels.team` all read `checkout`. The `RoleBinding`s
kept their original `creationTimestamp`, so they really were updated rather
than replaced. Access followed: testing by impersonation (which tests RBAC, not
the identity provider), `payments` could list pods in `svc-hello` before and
not after, `checkout` the other way round.

**The three cloud IAM carriers did not move, and nothing said so.** This is the
finding C-06 exists to produce, so it is written out plainly. The three IAM
members — Artifact Registry writer, `roles/cloudsql.client`,
`roles/cloudsql.instanceUser` — kept their object names, so upjet was asked to
change `member` on an existing external resource. (upjet is the code generator
that wraps the Terraform GCP provider as a Crossplane provider — it is what
`provider-upjet-gcp` is.) An IAM member cannot change
its member in place; Terraform would replace it; and **upjet does not perform
replacements.** The update was refused, permanently:

```
async update failed: refuse to update the external resource because the following update requires replacing it
```

The external names still said `payments`, and the registry still granted write
access to `payments` only. Worse than the failure is how quiet it was: a
managed resource's `Ready` condition is not re-evaluated by a failed update, so
all three stayed `Ready=True` from their original creation and only `Synced`
went False. `function-auto-ready` judges Ready, not Synced — so the `System`
reported `Ready=True` throughout, and Argo CD showed nothing wrong. **By every
signal the platform exposes, the move had succeeded.** Re-created: 0. Stuck: 3.

**The fix makes "re-created" literal.** `platform-config` PR #6 puts the team
into the object *name* and composition-resource-name of exactly those three, so
a move composes three new members and Crossplane garbage-collects the three old
ones — nothing is asked to mutate what it cannot. After that synced, the three
`-checkout` members reported `Ready=True`.

**The clean re-run (2026-09-17).** The move was run again, the other way
(`checkout` → `payments`), under the fixed Composition: one file, one line,
merged 11:42:01; by 11:43:56 the three `-payments` members were Ready, the
three `-checkout` members had been garbage-collected with no stuck deletes,
and the cloud policy showed `payments` on both Cloud SQL roles and the
registry. **One file, three re-created, 1m55s from merge.** The stale
`payments` grants the first run left behind were removed by hand that morning.

**What the first run's leftovers did overnight — and the hazard they
exposed.** The three original members, stuck deleting, eventually finished on
their own. Because the refused update had already rewritten their spec to
`checkout`, what they deleted was the *checkout* grants — while every
`-checkout` member object, in both tenant namespaces, still said Ready. The
mechanism is general: a project-level IAM binding is identified by role and
member, and two Systems owned by the same team each compose their own object
for that one cloud grant. Remove either and the grant is gone for both until
the provider's next poll restores it. The clean re-run reproduced it on
demand: moving `svc-hello` away from `checkout` took `svc-ledger`'s Cloud SQL
grants with it for about five minutes. **If you move a System between teams
today, expect the team it leaves to lose database login on its other Systems
for a few minutes.** The fix — a per-System IAM Condition on those grants —
is decided in the design seed's ADR-0016 §2 and is not built yet.

**The prediction, and how it scored.** The Composition's header predicted, in
writing and before the run: files touched 1, re-created 3 — the registry IAM
member and the two project IAM members. It was right about *which three* and
wrong about *how*: it assumed a replacement would happen, and the provider
refuses to replace. `RoleBinding.subjects` and `AppProject.spec.roles` did
update in place as predicted. Anything else showing up in the "re-created"
column is still a finding.

## Removing a System

Deleting the file is **not** the clean inverse of adding it, and the
difference is deliberate (ADR-0015).

Argo CD syncs this directory with prune and self-heal, so removing a tenant
file deletes the `System` XR, and with it everything the Composition fully
manages: the namespace and its contents, the `ResourceQuota`, the
`RoleBinding`, the Kubernetes and Google service accounts, every IAM member,
the `AppProject` and the `Application`.

It does **not** delete the durable resources — the Artifact Registry
repository, and any Cloud SQL instance or database the tenant claimed. Those
are composed with `managementPolicies` that omit `Delete`, plus deletion
protection on both the provider and the Cloud SQL side (ADR-0015 §1), so a
claim deletion cannot remove them. Removing them for real is a deliberate
`gcloud` act, performed after the claim is gone and recorded; the platform
offers no automation for it in M2 (ADR-0015 §5). Orphans are findable: every
durable resource carries a `system` label, and one whose `System` no longer
exists is the list the runbook works from.

Re-adding a file with the same name **adopts** the surviving repository and
instance rather than creating a second one — the Composition sets a
deterministic `crossplane.io/external-name` derived from the System's name
(ADR-0015 §2). That is the same mechanism that makes a cluster rebuild adopt
instead of duplicate, which is also why a System's name is immutable.

## The one manual prerequisite

**The team's Google Group must already exist, and must be nested under
`gke-security-groups@thecloudgeek.io`.**

This is a Google Workspace admin task and it is outside the paved road in M2
— stated here rather than hidden, because it is the one step a platform
engineer cannot do from a PR.

**How it was actually done on 2026-09-16**, because the commands are not quite
the obvious ones. The Cloud Identity API had to be enabled by hand first
(`gcloud services enable cloudidentity.googleapis.com`), and then **every**
`gcloud identity groups` call needed an explicit `--billing-project`: Cloud
Identity bills a quota project, and this identity's *default* quota project
resolves to a project it cannot use, so without the flag the calls fail for a
reason that has nothing to do with groups. The full command list lives in
`platform-bootstrap`'s README, Runbook step 5A. Three groups were created —
`gke-security-groups@`, `payments@`, `checkout@` — with the two team groups
nested inside the umbrella.

One wrinkle to clean up after: creating the umbrella group makes the creator a
direct `OWNER`/`MEMBER` of it, and GKE's rule for `gke-security-groups` is that
it contains groups only. That direct membership was still in place at the end
of the day and should be removed. A related trap, learned the same day: a
project **owner** cannot be the test subject for any of this, because IAM
grants an owner everything regardless of what RBAC says.

The details that matter:

- The umbrella group's name must be exactly `gke-security-groups`. GKE
  requires that literal local part; it is not a convention.
- Team groups are **nested groups** of the umbrella, never individual users.
  GKE resolves a user's access by checking whether they are in a group that
  is nested under the umbrella.
- Both the umbrella and each team group need the "View Members" permission
  set for group members, or GKE cannot read the membership.
- Group names in RBAC are **case-sensitive**, and the Composition uses the
  team field verbatim.
- Membership changes take a few minutes to propagate, plus up to an hour of
  credential caching. When testing a handover, expect to wait.
- Deleting one of these groups later breaks every binding that references it.

Once a team group is nested under the umbrella, its members can *authenticate*
to the cluster and see nothing. GKE requires `container.clusters.get` before
any RBAC is evaluated, and `platform-bootstrap`'s layer 0 grants
`roles/container.clusterViewer` once, to the umbrella group. So the
*authentication* hop is granted once and a team handover never touches it —
the only cloud IAM a handover does move is the three grants the Composition
manages itself (Artifact Registry writer, and the two Cloud SQL login roles),
and the platform's own provider identity holds project-IAM-admin under an IAM
Condition that permits exactly those two Cloud SQL roles and nothing else
(ADR-0013 §6).

**Half of that was confirmed on 2026-09-16 and half is unresolved.** With a
real user token, `kubectl auth whoami` returned groups
`[gke-security-groups@, payments@, checkout@, system:authenticated]` — so GKE
really does resolve **nested** Google Groups from a real login, which is the
mechanism this whole design rests on [C]. But the second test identity — a
non-owner, external consumer account, nested two groups deep — was refused at
the cluster's DNS endpoint with HTTP 403, and `gcloud container clusters
describe` said `Required "container.clusters.get"`, and was still refused on
2026-09-17, about nineteen hours after the membership was added — so this is
not propagation delay. Google's own Cloud Asset analyzer disagrees: `gcloud
asset analyze-iam-policy --expand-groups` lists that account as holding
`container.clusters.get` through `group:gke-security-groups@` on
`roles/container.clusterViewer`. Policy Troubleshooter answers
`MEMBERSHIP_UNKNOWN_INFO_DENIED`. **The analyzer says yes and the runtime says
no, and that is UNRESOLVED** — so treat "nest the group and the member gets in"
as verified for a normal org identity and open for an external account.

Bringing group creation inside the platform is possible later — the provider
family has a `cloudidentity.Group` kind — but that provider is not installed
and the Cloud Identity API is off.

## Part of the Platform Factory

This repo is one of seven that make up the reference implementation of the
**Platform Factory** pattern. The design seed — pattern docs, ADRs, and the
build plan — lives at [https://github.com/thecloudgeek/platform-factory](https://github.com/thecloudgeek/platform-factory).

This repo is built out in **M2**.

## Status

**Status:** M2 — live since 2026-09-16. Both tenants are synced and Ready.
`svc-hello` was the first System the platform ever composed (Ready 16:55:04);
`svc-ledger` was onboarded as the C-05 test by merging one file (merge → Ready
4m08s); `svc-hello` was then moved from `payments` to `checkout` as the C-06
test by changing one line.

C-05 has a clean result. C-06 has two runs: the first moved nothing in the
cloud while reporting success, and forced a design change; the clean re-run on
2026-09-17 did what was predicted — one file, three grants re-created, under
two minutes — and exposed the shared-grant hazard. `svc-hello` is owned by
`payments` again as of that re-run. All of it is written up in *Moving a
System between teams* above; grades are in the design seed's build log.

Two prerequisites the first sync leaned on are still manual and still outside
this repo: the Google Groups, and one `gcloud sql users create` per database
while provider-upjet-gcp #1000 is open. Results and grades go to the design
seed repo's `docs/build-log/m2-paved-road.md`.
