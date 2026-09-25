# AIMessage — container image.
#
# Default build is LIGHT: the pure-stdlib core (only pynacl), which runs the socket demo and the
# CI test suite. The real libp2p transport is heavy (trio/grpcio/aioquic + a C toolchain), so it's
# opt-in via  --build-arg WITH_LIBP2P=1.
#
# Build (light):   docker build -t aimessage .
# Run demo:        docker run --rm aimessage
# Node CLI:        docker run --rm aimessage aimessage id
# With libp2p:     docker build --build-arg WITH_LIBP2P=1 -t aimessage:libp2p .
#                  docker run --rm aimessage:libp2p python demo/demo_gossip.py
FROM python:3.12-slim

WORKDIR /app
ARG WITH_LIBP2P=0

# Install the package from pyproject (light = just pynacl). Needs the sources + README present.
COPY pyproject.toml README.md ./
COPY aimessage/ ./aimessage/
RUN pip install --no-cache-dir . \
 && if [ "$WITH_LIBP2P" = "1" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends build-essential libgmp-dev \
        && pip install --no-cache-dir ".[libp2p]" \
        && apt-get purge -y build-essential \
        && apt-get autoremove -y \
        && rm -rf /var/lib/apt/lists/* ; \
    fi

COPY demo/ ./demo/
COPY tests/ ./tests/

# Proof-of-life default: the self-contained socket demo. The `aimessage` console script is also on
# PATH (installed from pyproject) — e.g. `docker run ... aimessage serve --lan ...`.
CMD ["python", "demo/demo.py"]
