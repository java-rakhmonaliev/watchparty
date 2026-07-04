# Watch Party — deployment plan (AWS + Terraform + GitHub Actions)

This is the runbook for getting the app onto AWS and keeping it there with zero
manual server babysitting. It is a **plan first** — each phase below is a small,
verifiable step; nothing past Phase 0 has been built yet.

## Why this shape

The app is a great fit for the *smallest possible* footprint, and a bad fit for
the fancy stuff — pick the architecture that matches the workload:

- **Stateless by design.** Room state lives in Redis with 24 h TTLs; the video
  files never touch the server; there is no database, no user accounts, no
  uploads. Losing the instance loses nothing but live rooms.
- **Tiny compute needs.** The server only relays small JSON over WebSockets;
  video decode happens on clients and cam/voice is P2P. One small instance
  handles hundreds of concurrent rooms.
- **TURN disqualifies the "serverless" options.** coturn needs host networking
  and a UDP relay port range (49160–49200). That rules out App Runner and makes
  ECS/Fargate awkward (awkward port ranges, no host mode on Fargate). A plain
  EC2 instance running the **existing, already-tested docker-compose stack**
  (web + redis + caddy + coturn) is the honest choice.
- **Not chosen, deliberately:** EKS (absurd overkill), ECS Fargate (TURN + cost,
  and we'd re-solve what compose already solves), Lightsail (fine, but worse
  Terraform/IAM story than EC2 for the same money).

## Target architecture

```
GitHub repo ──push──> GitHub Actions
                       ├── CI: manage.py check + ws_smoke (32) + sync_sim (20),
                       │       in BOTH store modes (memory + Redis service)
                       ├── Build: docker buildx (arm64) ──> ECR
                       └── Deploy: SSM send-command ──> EC2

Route53 A record ──> Elastic IP ──> EC2 t4g.small (Amazon Linux 2023, arm64)
                                     └── docker compose
                                          ├── caddy   :80/:443  (TLS via Let's Encrypt)
                                          ├── web     daphne ASGI (image from ECR)
                                          ├── redis   (internal only)
                                          └── coturn  :3478 + UDP 49160–49200 (host net)
```

- **No SSH.** Instance access and deploys go through SSM Session Manager /
  `send-command`; the security group opens only 80, 443, 3478 (tcp+udp) and the
  UDP relay range.
- **No long-lived AWS keys in GitHub.** Actions authenticates via the GitHub
  OIDC provider assuming a scoped IAM role.
- **Secrets** (`SECRET_KEY`, TURN credentials) live in SSM Parameter Store
  (SecureString); the deploy step renders them into the instance's `.env`.
  GitHub holds only the OIDC role ARN and non-secret config (region, domain).

## Repository layout to be added

```
.github/workflows/
  ci.yml          # checks + smoke + sim on every PR/push
  deploy.yml      # main only: build -> ECR -> SSM deploy (after CI green)
infra/terraform/
  backend.tf      # S3 state bucket + DynamoDB lock (bootstrapped once, by hand)
  main.tf         # provider, default-VPC data sources
  ec2.tf          # instance + EIP + user_data (installs docker, logs into ECR,
                  #   writes .env from SSM params, compose up)
  sg.tf           # 80/443 tcp, 3478 tcp+udp, 49160–49200 udp, egress all
  iam.tf          # instance role: SSMManagedInstanceCore + ECR read + params read
  oidc.tf         # GitHub OIDC provider + deploy role (ECR push, SSM send-command)
  route53.tf      # A record -> EIP (zone assumed to exist)
  ecr.tf          # repository + lifecycle (keep last 10 images)
  variables.tf / outputs.tf
docker-compose.prod.yml   # override: web uses the ECR image instead of build:
```

## Phases (each one is a stop-and-verify milestone)

**Phase 0 — repo prep.** *(done in this session)* `git init`, `.gitignore`
(internal docs — DEVLOG/CLAUDE/.claude — plus env, venv, Terraform state stay
local), `.dockerignore` tightened, this plan. Next: create the GitHub repo,
first commit, push. **Owner input needed:** AWS account + region, and the
domain (bought where? if not Route53, either move NS or point an A record
manually and skip `route53.tf`).

**Phase 1 — Terraform bootstrap.** Hand-create (or one-off script) the S3 state
bucket + DynamoDB lock table; write `backend.tf`, `main.tf`, `variables.tf`.
Verify: `terraform init && terraform plan` runs clean locally with an empty
plan.

**Phase 2 — core infra.** ECR, security group, IAM, EC2 + EIP + user_data,
Route53. `user_data` does: install docker + compose plugin, ECR login helper,
fetch SSM params → `/opt/watchparty/.env`, clone-free deploy (compose files are
copied by the deploy step, not git on the server), `docker compose up -d`.
Verify: `https://DOMAIN` serves the app with a valid cert; two browsers sync;
`aws ssm start-session` works; **no SSH port open**.

**Phase 3 — CI (`ci.yml`).** On every PR/push: Python setup, `manage.py check
--deploy`, then the real test suites against a live `runserver` — once with the
in-memory store, once with `REDIS_URL` pointing at a `redis:7` service
container, plus `sync_sim.mjs` and `node --check` on the rendered pages. This
permanently closes the "Redis path not re-tested before deploy" gap from the
dev log. Verify: a PR that breaks the consumer contract goes red.

**Phase 4 — CD (`deploy.yml`).** On push to `main`, after CI: buildx an
**arm64** image (t4g is Graviton) tagged `sha-<short>` + `latest`, push to ECR,
then `aws ssm send-command`: pull the new tag, `docker compose -f
docker-compose.yml -f docker-compose.prod.yml up -d web`, prune old images.
Rollback = re-run the workflow pinned to a previous sha tag. Verify: merge a
visible change; it's live in <5 min with no dropped WebSockets beyond the web
container restart (~seconds; clients auto-reconnect with backoff).

**Phase 5 — TURN.** Enable the compose `turn` profile on the instance, set
`external-ip` in `turnserver.conf` to the EIP, open the SG ports (already in
`sg.tf`), set `TURN_*` params. Verify: two peers on different networks (one on
mobile hotspot) get cam/voice; `turnutils_uclient` succeeds against the relay.

**Phase 6 — observability + guardrails.**
- CloudWatch agent: instance metrics + docker logs (`awslogs` driver) with 2-week
  retention; alarm on instance status-check fail + disk >80 % → email/SNS.
- Uptime: Route53 health check (or a free external pinger) on `https://DOMAIN`.
- Billing alarm on the account (~$40/mo threshold, this stack should sit well
  under it).
- App hardening that becomes relevant with a public URL, in order: WebSocket
  origin validation (README documents why it's currently off — test harnesses;
  gate it behind an env flag so CI stays green), simple per-IP rate limiting on
  the WS endpoint, zombie-host reaping.

## Cost estimate (eu/us regions, monthly)

| Item | ~Cost |
|---|---|
| EC2 t4g.small on-demand | $12 |
| EBS 20 GB gp3 | $2 |
| Public IPv4 (EIP) | $4 |
| Route53 hosted zone + queries | $1 |
| ECR, S3 state, CloudWatch, SSM | ~$2 |
| **Base total** | **~$20/mo** |

TURN egress is the only variable: relayed cam/voice ≈ 0.3–0.6 GB/hour per
relayed pair at 320×240 (@ $0.09/GB ≈ a few dollars/month at friends-scale).
If usage stays trivial, a 1-year t4g.small reserved instance later cuts the
base ~40 %.

## Operational notes

- **State on the box is disposable.** Redis is room state only (TTL'd); Caddy's
  cert store lives in a named volume so restarts don't re-hit Let's Encrypt
  rate limits. Rebuilding the instance from scratch = `terraform apply` +
  re-run deploy (~10 min), certs re-issue automatically.
- **`--scale web=2` already works** (Redis-backed store + channel layer) if one
  worker ever saturates — same box, no infra change.
- **What would force a real re-architecture:** >5-person rooms (SFU — explicitly
  out of scope) or thousands of concurrent rooms (move Redis to ElastiCache,
  web to an ASG behind an ALB with sticky WebSockets). Neither is planned.
- **Maintenance cadence:** AL2023 auto-applies security patches; monthly
  `docker compose pull` for redis/caddy/coturn minors (can be a scheduled
  Action via SSM); Django/Channels bumps arrive as Dependabot PRs that must
  pass the full CI matrix before merge.
