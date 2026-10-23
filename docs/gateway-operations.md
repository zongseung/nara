# Gateway and memory operations

The deployed HTTP service is the Prefect operator UI/API. Product matching and workbook processing remain CLI operations. Nginx fronts Prefect; it is not an employee product-search service. Only the gateway publishes `127.0.0.1:${PREFECT_HOST_PORT:-4200}`. Internal workers continue using `http://prefect-server:4200/api`.

## Startup and access

On a new checkout, copy `docker/.env.example` to `docker/.env`, replace both credential placeholders, and set its permissions to 600. The file is excluded from Git and the Docker build context. Then run from the repository root:

```sh
docker compose up -d --build
```

Operator access from another machine can use `ssh -L 4200:127.0.0.1:4200 USER@INTERNAL_SERVER`, then open `http://127.0.0.1:4200`. The host port is configurable. For a company-wide URL, terminate HTTPS at the company's trusted gateway and forward to this loopback entrypoint. Restrict the Prefect operator interface to operators; Basic auth over plain LAN HTTP is not a deployment option. Certificates, DNS and company authentication require the actual company configuration and are not supplied here.

Root `compose.yaml` includes `docker/compose.yaml`; Docker Compose 2.20.3 or newer is required ([Docker include documentation](https://docs.docker.com/compose/how-tos/multiple-compose-files/include/)). Docker-specific files, fixtures, dependency locks and environment templates live in `docker/`. The default command starts the registration job automatically and waits for its success before starting the worker and dispatcher. A new installation stays paused with an inactive schedule and both source/dispatcher flags false. Repeated startup preserves existing operator activation.

The gateway image bundles `docker/nginx.conf`; rebuild and recreate `gateway` after changing it. Its base image is digest-pinned. Docker DNS is re-resolved every ten seconds so recreating the backend does not leave Nginx pointing permanently at an old IP. WebSocket upgrades and Prefect authentication are passed through. `/api/health` checks the backend without consuming the client rate budget.

## Initial limits

| Component | Default | Configuration |
|---|---|---|
| Redis dataset | 128 MiB, `noeviction`, at most 256 clients | `NARA_REDIS_MAXMEMORY=128mb` |
| Redis container | 512 MiB, no extra swap | `NARA_REDIS_MEMORY=512m` |
| Prefect API/services and PostgreSQL, each | 768 MiB, no extra swap | `NARA_CONTROL_MEMORY=768m` |
| Worker including child processes | 2 GiB, no extra swap; one flow at a time | `NARA_WORKER_MEMORY=2g` |
| Dispatcher | 256 MiB, no extra swap | Compose |
| Gateway | 128 MiB; temporary files at most 64 MiB | Compose |
| Gateway client traffic | 30 requests/second/IP, burst 60; 32 concurrent requests/IP, 128 total | `docker/nginx.conf` |
| Request body | At most 8 MiB, 15-second header/body inactivity timeout | `docker/nginx.conf` |
| Proxy | 3-second connect, 60-second send, 300-second read inactivity timeout | `docker/nginx.conf` |
| Container logs | Local driver, 3 files of at most 10 MiB per container | Compose runtime defaults |

These are starting budgets, not measured production capacity. The normal always-on services can together consume about 5 GiB up to their configured limits, plus host/engine overhead; size the host and adjust limits after observing representative jobs. A process exceeding its hard limit can be killed. This contains resource use but does not fix a Python memory leak or make arbitrary workbook sizes fit. The existing worker concurrency and bounded page collection remain in effect. Clients behind one NAT share the per-IP budget. Excess traffic receives 429 rather than an unbounded queue.

## Cache and durable data

The existing Redis is the Prefect event bus and deduplication cache, not the catalog database. Keep its `noeviction` policy: on dataset exhaustion, growing writes fail while existing data stays readable. Treat this as an operational failure, investigate consumer progress, then adjust the budget. Do not `FLUSHALL`, expire stream keys manually, or apply `allkeys-lru` to this instance. Prefect-Redis performs its own consumed-stream trimming; stopped consumers, pending events and dead letters can still accumulate. Redis uses AOF with `appendfsync everysec`; there is no additional scheduled RDB snapshot. Redis is not the business backup.

Keep Redis dataset memory below the container limit with room for allocator, clients, persistence buffers and rewrite overhead. `maxmemory` is not an RSS limit. See [Redis memory and eviction](https://redis.io/docs/latest/develop/reference/eviction/).

Nginx response caching is disabled and responses carry `Cache-Control: no-store`. Authenticated responses, mutable status and current-price decisions must not be reused from a proxy cache. Nginx streaming buffers and temporary storage are bounded. The product source of truth remains published SQLite snapshots and its evidence ledger. Matching indexes are disk files, not Redis keys; existing snapshot retention does not automatically prune every old matching index. Proxy/cache settings do not bound historical database or index disk usage. Use the documented maintenance and backup procedures before removing retained data.

If a product HTTP search service is later introduced, give its disposable cache a separate Redis instance with a bounded dataset, TTL and eviction policy. Key entries by source snapshot/generation and query/policy/model inputs; an update must change the cache key. No such unused application cache is added to this deployment.

## Diagnostics and validation

```sh
docker compose exec -T gateway nginx -t
docker compose exec -T redis redis-cli INFO memory
docker compose exec -T redis redis-cli INFO stats
docker compose exec -T redis redis-cli CONFIG GET maxmemory maxmemory-policy
docker compose stats --no-stream
```

Watch dataset/RSS growth, `errorstat_OOM` (`INFO errorstats`), client count, pending stream entries, consumer progress, container restarts/OOM kills, disk free space, and gateway 429/502/504 rates. Gateway access logs exclude query strings and Authorization headers; do not dump environment secrets into incident reports. Rate and connection limits are admission controls; a single backend remains a single backend.

Offline Compose guards run with `python -m unittest test.test_updater_compose`. After starting a separate QA stack on port 14201, run `NARA_GATEWAY_QA=1 NARA_GATEWAY_QA_ENV_FILE=/protected/qa.env python -m unittest test.integration.test_gateway -v` in the complete updater environment. The five opt-in tests verify authentication, same-origin UI settings, mutation visibility without cached responses, WebSocket authentication, body size and request bursts. Never point them at production: they write temporary Prefect variables and deliberately generate a burst of requests.

References: [Prefect reverse proxy and UI configuration](https://docs.prefect.io/v3/advanced/self-hosted#reverse-proxy-configuration), [Nginx WebSockets](https://nginx.org/en/docs/http/websocket.html), [Nginx request limiting](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html).
