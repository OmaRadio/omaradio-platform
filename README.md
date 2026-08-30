# Welcome to the OmaRadio Platform

An Omarchy Linux focused pirate radio platform, broadcasting live via
"Transmitter-One" from an undisclosed location in the Falkland Islands.

Reachable online via https://OmaRadio.stream

For a full picture of what this is about, see [`The-Spirit-of-OmaRadio.md`](./The-Spirit-of-OmaRadio.md).

The idea is to create a fully-autonomous radio platform featuring unique on-air dj segments, multi-genre Creative Commons music and most importantly up-to-date news and happenings from the community.

## Platform Roles

The platform uses the following roles to define responsibilities, determine task assignment, etc.

- **Orchestrator** — the human guiding the platform's overall direction.
- **Station Manager** — one per station; owns creative direction, scheduling, and DJ coordination.
- **DJ** — on-air voice personalities; write and perform their own segments.
- **Intern** — sources and screens Omarchy news/content for DJs and Station Managers.
- **IT Guy** — owns infrastructure: website, streaming stack, cloud, servers, code.

## Directory Structure

```
omaradio-platform/
├── branding/           Platform and station brand assets
├── www/                Public marketing website featuring streaming web player, segment schedule and info about each dj
├── scheduler/           Task dispatch and agent orchestration service
│   ├── src/              Application source
│   └── db/               Schema and migrations
├── infra/               Infrastructure and deployment
│   └── transmitter/
│       ├── provisioning/   Server/environment provisioning
│       └── deploy/         Deploy configs (compose, per-station settings)
└── agents/               Agent definitions, one directory per role
    ├── it-guy/
    └── stations/
        └── prime/         Per-station agents (station-manager, intern, djs)
```

Only `prime` exists today; additional stations follow the same pattern under
`agents/stations/<station>/` and `infra/transmitter/deploy/stations/<station>/`.
