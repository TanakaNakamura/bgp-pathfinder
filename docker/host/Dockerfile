# Lightweight end host for the lab (client / target).
FROM alpine:3.23.8

# Package versions come from the pinned Alpine 3.23 branch (see docs/DECISIONS.md).
RUN apk add --no-cache \
        iputils-ping \
        traceroute \
        curl \
        iperf3 \
        iproute2 \
        busybox-extras \
        tcpdump \
    && mkdir -p /www \
    && echo "target 198.51.100.10 ok" > /www/index.html

# client only: data-plane metrics for the Grafana dashboard
COPY path-exporter.sh /usr/local/bin/path-exporter.sh
RUN chmod 0755 /usr/local/bin/path-exporter.sh

CMD ["sleep", "infinity"]
