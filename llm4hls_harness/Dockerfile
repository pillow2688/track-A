# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89

# Vitis 2025.2 is proprietary and is deliberately not copied into this image.
# This digest is the linux/amd64 manifest for python:3.12.11-slim-bookworm.
FROM python:3.12.11-slim-bookworm@sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49

LABEL org.opencontainers.image.title="LLM4HLS V3-D agent runtime" \
      org.opencontainers.image.description="Agent-only clean-room runtime; AMD Vitis is supplied externally" \
      org.opencontainers.image.source="https://github.com/pillow2688/track-A"

ENV HOME=/tmp \
    LLM4HLS_OUTPUT_DIR=/outputs \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/llm4hls \
    PYTHONUNBUFFERED=1

WORKDIR /opt/llm4hls

# These are only the minimal userspace prerequisites for the read-only
# ``vitis-run --version`` preflight. The proprietary Vitis tree stays external.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
      libx11-6=2:1.8.4-2+deb12u2 \
      locales=2.36-9+deb12u14 \
    && sed -i 's/^# en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8

COPY requirements-v3.lock ./requirements-v3.lock
RUN python -m pip install \
      --disable-pip-version-check \
      --no-cache-dir \
      --require-hashes \
      --requirement requirements-v3.lock

COPY llm4hls_agent ./llm4hls_agent
COPY task_corpus/v3d-fast ./task_corpus/v3d-fast
COPY scripts ./scripts

RUN chmod 0555 scripts/v3d-reproduce.sh scripts/vitis-2025.2-preflight.sh \
    && mkdir -p /outputs \
    && chown 1000:1000 /outputs \
    && python -c "import langgraph, llm4hls_agent.v3_batch_benchmark"

# The numeric user keeps the image independent of host account databases. The
# documented bind mount is writable by the usual desktop uid/gid 1000:1000;
# callers with a different uid should add --user "$(id -u):$(id -g)".
USER 1000:1000
VOLUME ["/outputs"]

ENTRYPOINT ["/opt/llm4hls/scripts/v3d-reproduce.sh"]
CMD ["demo-smoke"]
