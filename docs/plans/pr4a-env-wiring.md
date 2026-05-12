# PR #4a appendix — per-service env wiring

Source: production cluster (canonical GKE, inspected 2026-05-12).
All Pod env-vars come from `Secret/voipbin` in ns `bin-manager` unless noted.
Renames (Pod env name ≠ Secret key name) marked with ← renamed.

## agent-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 8

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## ai-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 9

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `ENGINE_KEY_CHATGPT` | `OPENAI_API_KEY` | yes |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## api-manager
- ports (containerPort, name): [[2112, 'metrics'], [443, 'service'], [9000, 'audiosocket']]
- env count: 15

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `SSL_PRIVKEY_BASE64` | `SSL_PRIVKEY_API_BASE64` | yes |
| `SSL_CERT_BASE64` | `SSL_CERT_API_BASE64` | yes |
| `GCP_PROJECT_ID` | `GCP_PROJECT_ID` | — |
| `GCP_BUCKET_NAME` | `GCP_BUCKET_NAME_TMP` | yes |
| `JWT_KEY` | `JWT_KEY` | — |

fieldRef envs: `POD_NAME`=metadata.name, `POD_NAMESPACE`=metadata.namespace, `POD_IP`=status.podIP

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## billing-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 11

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |
| `PADDLE_API_KEY` | `PADDLE_API_KEY` | — |
| `PADDLE_PRICE_ID_BASIC` | `PADDLE_PRICE_ID_BASIC` | — |
| `PADDLE_PRICE_ID_PROFESSIONAL` | `PADDLE_PRICE_ID_PROFESSIONAL` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## call-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 15

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `HOMER_API_ADDRESS` | `HOMER_API_ADDRESS` | — |
| `HOMER_AUTH_TOKEN` | `HOMER_AUTH_TOKEN` | — |
| `HOMER_WHITELIST` | `HOMER_WHITELIST` | — |
| `PROJECT_BUCKET_NAME` | `GCP_BUCKET_NAME_MEDIA` | yes |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

fieldRef envs: `NODE_IP`=status.hostIP, `POD_IP`=status.podIP

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`, `PROJECT_BASE_DOMAIN=<brand-domain>` (install repo templates to `PLACEHOLDER_DOMAIN`)

## campaign-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 8

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## conference-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 8

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## contact-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 8

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## conversation-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 8

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## customer-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 8

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## direct-manager
- ports (containerPort, name): [[2112, 'metrics'], [80, 'grpc']]
- env count: 9

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |

fieldRef envs: `NODE_IP`=status.hostIP, `POD_IP`=status.podIP

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## email-manager
- ports (containerPort, name): [[2112, 'metrics'], [80, 'grpc']]
- env count: 12

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `SENDGRID_API_KEY` | `SENDGRID_API_KEY` | — |
| `MAILGUN_API_KEY` | `MAILGUN_API_KEY` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

fieldRef envs: `NODE_IP`=status.hostIP, `POD_IP`=status.podIP

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## flow-manager
- ports (containerPort, name): [[2112, 'metrics'], [80, 'grpc']]
- env count: 10

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

fieldRef envs: `NODE_IP`=status.hostIP, `POD_IP`=status.podIP

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## hook-manager
- ports (containerPort, name): [[2112, 'metrics'], [443, 'service-https'], [80, 'service-http']]
- env count: 10

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `SSL_PRIVKEY_BASE64` | `SSL_PRIVKEY_HOOK_BASE64` | yes |
| `SSL_CERT_BASE64` | `SSL_CERT_HOOK_BASE64` | yes |
| `PADDLE_WEBHOOK_SECRET_KEY` | `PADDLE_WEBHOOK_SECRET_KEY` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## message-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 10

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `AUTHTOKEN_MESSAGEBIRD` | `AUTHTOKEN_MESSAGEBIRD` | — |
| `AUTHTOKEN_TELNYX` | `TELNYX_TOKEN` | yes |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## number-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 13

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `TWILIO_SID` | `TWILIO_SID` | — |
| `TWILIO_TOKEN` | `TWILIO_TOKEN` | — |
| `TELNYX_CONNECTION_ID` | `TELNYX_CONNECTION_ID` | — |
| `TELNYX_PROFILE_ID` | `TELNYX_PROFILE_ID` | — |
| `TELNYX_TOKEN` | `TELNYX_TOKEN` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## outdial-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 8

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## pipecat-manager
- ports (containerPort, name): [[2112, 'metrics'], [8080, 'audiosocket']]
- env count: 16

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CARTESIA_API_KEY` | `CARTESIA_API_KEY` | — |
| `ELEVENLABS_API_KEY` | `ELEVENLABS_API_KEY` | — |
| `OPENAI_API_KEY` | `OPENAI_API_KEY` | — |
| `DEEPGRAM_API_KEY` | `DEEPGRAM_API_KEY` | — |
| `XAI_API_KEY` | `XAI_API_KEY` | — |
| `GOOGLE_API_KEY` | `GOOGLE_API_KEY` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

fieldRef envs: `NODE_IP`=status.hostIP, `POD_IP`=status.podIP

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## queue-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 8

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## rag-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 8

| Pod env-var | Secret key | rename? |
|---|---|---|
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `GCP_PROJECT_ID` | `GCP_PROJECT_ID` | — |
| `GCP_REGION` | `GCP_REGION` | — |
| `POSTGRESQL_DSN` | `DATABASE_DSN_POSTGRES` | yes |

literal envs: `GOOGLE_EMBEDDING_MODEL=text-embedding-004`, `RAG_TOP_K=5`, `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## registrar-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 11

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN_BIN` | `DATABASE_DSN_BIN` | — |
| `DATABASE_DSN_ASTERISK` | `DATABASE_DSN_ASTERISK` | — |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `DOMAIN_NAME_EXTENSION` | `DOMAIN_NAME_EXTENSION` | — |
| `DOMAIN_NAME_TRUNK` | `DOMAIN_NAME_TRUNK` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## route-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 9

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |
| `EXTERNAL_SIP_GATEWAY_ADDRESSES` | `EXTERNAL_SIP_GATEWAY_ADDRESSES` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## sentinel-manager
- ports (containerPort, name): [[2112, 'metrics'], [80, 'grpc']]
- env count: 4

| Pod env-var | Secret key | rename? |
|---|---|---|
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## storage-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 12

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `GCP_PROJECT_ID` | `GCP_PROJECT_ID` | — |
| `GCP_BUCKET_NAME_TMP` | `GCP_BUCKET_NAME_TMP` | — |
| `GCP_BUCKET_NAME_MEDIA` | `GCP_BUCKET_NAME_MEDIA` | — |
| `JWT_KEY` | `JWT_KEY` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## tag-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 8

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## talk-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 8

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## timeline-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 10

| Pod env-var | Secret key | rename? |
|---|---|---|
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |
| `CLICKHOUSE_DATABASE` | `CLICKHOUSE_DATABASE` | — |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `HOMER_API_ADDRESS` | `HOMER_API_ADDRESS` | — |
| `HOMER_AUTH_TOKEN` | `HOMER_AUTH_TOKEN` | — |
| `GCS_BUCKET_NAME` | `GCP_BUCKET_NAME_MEDIA` | yes |

fieldRef envs: `NODE_IP`=status.hostIP, `POD_IP`=status.podIP

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## transcribe-manager
- ports (containerPort, name): [[2112, 'metrics'], [8080, 'audiosocket']]
- env count: 14

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `AWS_ACCESS_KEY` | `AWS_ACCESS_KEY` | — |
| `AWS_SECRET_KEY` | `AWS_SECRET_KEY` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

fieldRef envs: `NODE_IP`=status.hostIP, `POD_IP`=status.podIP

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`, `STREAMING_LISTEN_PORT=8080`, `STT_PROVIDER_PRIORITY=GCP,AWS`

## transfer-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 10

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

fieldRef envs: `NODE_IP`=status.hostIP, `POD_IP`=status.podIP

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## tts-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 14

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `AWS_ACCESS_KEY` | `AWS_ACCESS_KEY` | — |
| `AWS_SECRET_KEY` | `AWS_SECRET_KEY` | — |
| `ELEVENLABS_API_KEY` | `ELEVENLABS_API_KEY` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

fieldRef envs: `POD_NAME`=metadata.name, `POD_NAMESPACE`=metadata.namespace, `POD_IP`=status.podIP

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`

## webhook-manager
- ports (containerPort, name): [[2112, 'metrics']]
- env count: 8

| Pod env-var | Secret key | rename? |
|---|---|---|
| `DATABASE_DSN` | `DATABASE_DSN_BIN` | yes |
| `RABBITMQ_ADDRESS` | `RABBITMQ_ADDRESS` | — |
| `REDIS_ADDRESS` | `REDIS_ADDRESS` | — |
| `REDIS_PASSWORD` | `REDIS_PASSWORD` | — |
| `REDIS_DATABASE` | `REDIS_DATABASE` | — |
| `CLICKHOUSE_ADDRESS` | `CLICKHOUSE_ADDRESS` | — |

literal envs: `PROMETHEUS_ENDPOINT=/metrics`, `PROMETHEUS_LISTEN_ADDRESS=:2112`
