# Oracle Always Free chat deployment

This deploys the browser grammar chat and local Ollama model on one Linux VM.
It does not run the S3/OpenAI ingestion API. It needs no AI API keys.
The existing OCR draft is mounted read-only and remains unreviewed.

## VM and networking

Create an Ubuntu ARM VM using `VM.Standard.A1.Flex`, with 2 OCPUs and
12 GB RAM, in your home region. Confirm the console shows the allocation
within your remaining Always Free allowance before creating it. A 50 GB boot
disk is a starting point; the allowance is shared with your other resources.
Capacity is not guaranteed and Oracle can reclaim idle free instances.

Assign a public IP. Allow inbound TCP 80 and 443 in the Oracle network security
group/security list and the VM firewall. Restrict SSH (TCP 22) to your own IP.
Keep ports 8001 and 11434 closed. Do not flush existing firewall rules.
Point a domain/subdomain A record at the VM public IP; remove any AAAA record
unless IPv6 is also configured. A free DNS subdomain can avoid domain costs.

Install Docker Engine with the Compose plugin using the official Ubuntu guide:
https://docs.docker.com/engine/install/ubuntu/

## Start

Transfer this project to the VM, including the ignored file
`output/pdf/textbook-extraction/textbook.json`. Do not transfer your local
`.env`, `.venv`, SSH keys, or AWS credentials. Run the following from the
project root on the VM (use sudo for Docker if your user lacks access):

```sh
cp deploy/oracle/.env.example deploy/oracle/.env
docker run --rm -it caddy:2.10.2-alpine caddy hash-password
```

Edit `deploy/oracle/.env`: set the domain (hostname only), login, and generated
password hash. Keep the hash in single quotes. Save the login password in your
password manager. Protect the environment file with `chmod 600 deploy/oracle/.env`.

```sh
docker compose --env-file deploy/oracle/.env -f deploy/oracle/compose.yaml config --quiet
docker compose --env-file deploy/oracle/.env -f deploy/oracle/compose.yaml up -d --build ollama chat
docker compose --env-file deploy/oracle/.env -f deploy/oracle/compose.yaml exec ollama ollama pull qwen2.5:3b
docker compose --env-file deploy/oracle/.env -f deploy/oracle/compose.yaml up -d proxy
```

Caddy obtains a public HTTPS certificate after DNS and ports are working.
The browser asks for your username and password before loading the app.
Models and certificates survive container restarts in Docker volumes.
The Compose file uses Linux host networking so the app and Ollama remain bound
to loopback. The proxy supplies the local Host header required by the app.

## Verify before considering deployment complete

```sh
curl -I https://YOUR_DOMAIN
curl --user vocard https://YOUR_DOMAIN/api/parts
docker compose --env-file deploy/oracle/.env -f deploy/oracle/compose.yaml logs --tail 50
```

The first request must return 401; the second prompts for your password and
should return textbook parts. Log in on your phone using mobile data, select a
chapter, and ask a question. Confirm that an answer is generated. Also confirm
ports 8001 and 11434 cannot be reached from outside the VM. CPU generation can
be slow; actual latency and ARM image/dependency compatibility must be tested
on the VM. This is a personal, password-protected service with browser-session
history, not a multi-user account system. Back up the source JSON separately.

References: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
and https://caddyserver.com/docs/caddyfile/directives/basic_auth
