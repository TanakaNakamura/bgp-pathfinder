# Optimizer node: ExaBGP 4.x with the optimizer running as an ExaBGP "process".
# Build context: repository root.
FROM python:3.12.16-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        iputils-ping \
        traceroute \
        iproute2 \
        procps \
    && rm -rf /var/lib/apt/lists/*

COPY docker/optimizer/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY pyproject.toml /src/pyproject.toml
COPY optimizer /src/optimizer
RUN pip install --no-cache-dir --no-deps /src && rm -rf /src

COPY docker/optimizer/exabgp.conf /etc/exabgp/exabgp.conf
COPY docker/optimizer/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/optimizer/run-optimizer.sh /usr/local/bin/run-optimizer.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/run-optimizer.sh \
    && mkdir -p /var/lib/optimizer /var/log/optimizer /etc/optimizer

# ExaBGP environment (equivalent to exabgp.env). Names verified against
# exabgp/configuration/setup.py of the installed 4.2.25.
ENV exabgp_daemon_user=root \
    exabgp_api_respawn=false \
    exabgp_api_cli=false \
    exabgp_api_ack=true \
    exabgp_log_destination=stdout \
    exabgp_log_level=INFO \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
